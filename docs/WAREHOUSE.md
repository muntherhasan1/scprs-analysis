# SCPRS data warehouse (medallion architecture)

`src/warehouse.py` builds a layered analytical warehouse in `data/warehouse.db`
from the operational store `data/scprs.db`. Rebuild is idempotent:

```bash
python -m src.warehouse build   # bronze -> silver -> gold + data-quality
python -m src.warehouse dq       # re-run data-quality checks
python -m src.warehouse info     # layer row counts + last batch
```

## Layers

### 🥉 Bronze — raw + lineage
Untransformed snapshots of each source table, stamped with load lineage
(`_batch_id`, `_loaded_at`, `_source`). Tables: `bronze_purchases`,
`bronze_document_details`, `bronze_document_lines`, `bronze_document_pos`.
Bronze is the immutable landing zone — nothing is cleaned here.

### 🥈 Silver — cleaned, conformed, typed
- **Grain resolution.** The source is at *(document, version)* grain; silver
  collapses to the **current version** so `silver_document` is one row per
  document. Line/PO rows are filtered to the current version too (a document can
  be drilled at several versions), then de-duplicated. Lines dedupe on
  **structure, not content**: a document drilled twice into one load appends
  its whole line set again (rows = one block repeated k times, in load order)
  and collapses to the first block, while a line PO Details genuinely lists on
  several schedules keeps every row. The header's `merchandise_amount` breaks
  the rare tie (one line repeated k times looks the same both ways): the first
  of structural / all rows / distinct rows that reconciles within $1 wins. The
  old content `DISTINCT` dropped 234 docs / $8.46M of real lines on the
  2026-10 snapshot (removing it outright would have re-added $2.50B of
  repeat-drill duplicates across 4,719 docs).
- **Merge precedence.** The PO Details drill-down overrides the summary only
  while its version is at least the summary's; a document amended after it was
  drilled takes the (daily-refreshed) summary for every shared field and is
  flagged `detail_is_stale = 1` (warn check `stale_drilldowns`). Drill-only
  fields (bill code, merchandise/freight) always come from the drill.
  **`grand_total` is the summary's at every version** (the figure an SCPRS
  search returns); the drill fills it only when the summary has none. At equal
  versions the two pages can still disagree: live-checked 2026-10-29, 3 of 8
  such drills were out of date and 5 were the site disagreeing with itself.
- **Conforming.** Amounts/dates cast to real types; the combined acquisition
  string — from the summary *and* the drill-down, which glues sub-types for
  NON-IT Services — is split into `acquisition_type` / `acquisition_sub_type`
  (error check `acquisition_type_split`); a `competitive_flag` is derived from
  the method's wording (legacy: it calls Fair and Reasonable competitive). The
  governed classification lives in gold: `dim_acquisition` carries each method's
  State Contracting Manual `scm_code`, `award_family` and `award_class`
  (`references/award_methods.csv`: C competitive, L leveraged agreement,
  N non-competitive, X not procurement, U unknown — warn check
  `award_methods_mapped`), `fact_document` carries a reviewed outlier's
  `award_class_override` / `contract_flag` (`references/contract_flags.csv`), and
  `gold_document.award_class` applies the override. `gold_procurement` =
  `gold_document` minus class X and class Q (a reviewed contract whose registered
  value is implausible, quarantined until SCPRS is corrected — override only); the supplier marts' `pct_noncompetitive_value`
  is class N over classes C+L+N. The drill-down's literal `N/A` placeholder (end date, LPA, buyer,
  UNSPSC) becomes NULL (error check `no_na_sentinels`).
- **Superseded registrations.** One agreement stored under a legacy number and again
  under its department-prefixed re-registration (CAL FIRE's FI$Cal conversion:
  `7CA03777` v0 and `35407CA03777`, amended and extended — both live in SCPRS) is
  counted once. `src/registrations.py` pairs them (prefixed number = unit code +
  legacy number, optional `FY…` suffix; same start day; same normalized supplier),
  plus hand-reviewed near-pairs in `references/superseded_registrations.csv`
  (same agreement number and subject; a renamed or successor supplier, or a start
  day that moved). The legacy record is left out of `fact_document`, `fact_line`
  and `fact_associated_po` — so out of every mart and total — and kept in the
  table `gold_superseded_registration` with what supersedes it and why. Error
  checks: `document_grain_parity` counts fact + superseded against bronze;
  `superseded_target_present` requires every superseding document in the fact.
  `reconcile.contracts` applies the same rule to the operational store.
- **Defaulting.** NULL business keys become explicit `Unknown`/`UNKNOWN` members
  (no NULL dimension keys downstream).
- **Quality flags.** `dq_line_reconciles` marks whether an enriched document's
  line items sum to its merchandise amount; `has_associated_pos` classifies
  contract vs standalone for *all* documents (drill-down POs are enriched-only).
- Tables: `silver_department` (conformed reference dimension from
  `references/departments.csv`), `silver_document`, `silver_line`,
  `silver_associated_po`.

### 🥇 Gold — Kimball star schema
Surrogate-keyed **conformed dimensions** and **fact tables** at declared grains,
plus mart views.

**Dimensions** (surrogate key + natural key + `dw_loaded_at`):
`dim_date` (date spine + Unknown member; calendar `year`/`quarter`/`month` plus
California `fiscal_year`/`fiscal_quarter` — FY runs Jul 1–Jun 30, labelled by the
year it ends in), `dim_department`, `dim_supplier`, `dim_buyer`,
`dim_acquisition`, `dim_unspsc`.

**Facts:**
| Fact | Grain | Key measures |
|---|---|---|
| `fact_document` | one purchase document (current version) | merchandise, freight/tax, grand_total, line_count, associated_po_count |
| `fact_line` | one document line item (enriched docs) | quantity, unit_price, line_amount |
| `fact_associated_po` | one associated PO transaction (enriched) | po_total |

`document_bk` is a degenerate dimension (the natural document key) carried on the
facts. FKs use COALESCEd naturals so every fact row resolves to a real dimension
member.

**Marts (views):** `gold_document` (COMPLETE document-grain mart — one row per
purchase document with grand_total + raw/canonical supplier + acquisition
taxonomy + department + `first_item_title` (the summary's description of the
first item — what the contract is for, on every document) +
`start_date`/`calendar_year`/`fiscal_year`; the primary
source for spend/supplier/category/time, since `gold_line_item` covers only the
~13% of documents that were line-enriched), `gold_supplier_spend`,
`gold_monthly_spend`,
`gold_acquisition_spend`, `gold_unspsc_spend`, `gold_contract_vs_standalone`,
`gold_line_item` (denormalized line items: free-text `item_description` + UNSPSC
category + price + vendor + `start_date`/`calendar_year`/`fiscal_year` + the
curated `acquisition_type`/`acquisition_sub_type` taxonomy, so
supplier×category×time questions resolve from this one mart),
`gold_acquisition_unspsc` (crosswalk: which UNSPSC line codes flow through each
acquisition type/sub-type — the curated taxonomy, e.g. `IT Services`, is often a
cleaner category than the free-coded line UNSPSC).

The free-text line description is a **degenerate attribute** on `fact_line`
(`item_description`; 79% unique and `item_id` is a constant placeholder, so it is
kept on the fact rather than modeled as a `dim_item`).

**Competitive-intelligence marts:**
- `gold_supplier_profile` — vendor scorecard (value, reach, % won non-competitively).
- `gold_supplier_share` — supplier share of each department's spend, per
  **canonical** supplier (`supplier_id`/`supplier_name` carry the canonical
  id/name).
- `gold_market_concentration` — HHI + top-supplier share per dept × acquisition
  type, over **canonical** suppliers (`supplier_count` counts companies). Until
  2026-10 both grouped by registration, which understates concentration (on the
  2026-10 snapshot 20 of 183 cells moved, by at most 20 points).
- `gold_price_benchmark` — unit-price spread per UNSPSC where 2+ suppliers compete.
- `gold_supplier_unspsc_profile` / `gold_supplier_specialization` — what each
  vendor supplies and how specialized.
- `gold_supplier_acquisition_profile` — broad (all-document) category footprint,
  per canonical supplier like `gold_supplier_share`.

**Canonical vendor (master data):** SCPRS issues a `supplier_id` per *registration*,
so one company can appear under several ids (NORTH RIDGE CONSULTING has two; BETA
ALPHA PSI four), splitting rollups and enrichment. `src/supplier_master.py` resolves
identities to a canonical entity via a curated, version-controlled crosswalk
(`references/supplier_master.csv`: `supplier_id → canonical_id/canonical_name`, plus
an optional `parent_name`). `build` adds `canonical_id`/`canonical_name`/`parent_name`
to `dim_supplier` (a supplier defaults to being its own canonical entity), enabling:
- `gold_canonical_supplier_spend` — spend rolled up to the canonical vendor;
  `registration_count > 1` flags a deduplicated vendor.
- `gold_supplier_master` — canonical vendor scorecard (deduped metrics + parent +
  web firmographics, matched by canonical name).

Seed/curate the crosswalk with `python -m src.supplier_master suggest [--write]`
(it proposes ids that share a normalized name, canonical = highest-spend id).

**Supplier enrichment:** web-researched firmographic profiles (org type, HQ,
certifications, ownership) with **source provenance + a confidence score** live
in a separate store `data/supplier_enrichment.db` (`src/supplier_research.py`).
The warehouse snapshots them to `bronze_supplier_web` and joins them to the
per-registration scorecard in **`gold_supplier_enriched`** and to the canonical
scorecard in **`gold_supplier_master`** (internal metrics + external firmographics
+ confidence, matched by supplier name).

**CMAS integration:** a second optional side input, California's CMAS
master-agreement contractors (`data/cmas.db`, `src/cmas.py`), is snapshotted to
`bronze_cmas` and matched to `dim_supplier` **by normalized name** — reusing
`supplier_master.normalize_name` (strip punctuation + legal suffixes), which
roughly triples the match rate over the plain `UPPER()` join. It exposes
**`gold_cmas_agreement`** (all agreements + a `matched_to_supplier` flag) and
**`gold_supplier_cmas`** (our canonical suppliers that also hold a CMAS agreement —
SCPRS spend beside CMAS terms / SB-DVBE / product reach). Absent `data/cmas.db`,
the build is a clean no-op. See `docs/CMAS.md`.
A normalized name shared by **2+ canonical ids** (an uncurated duplicate
registration) is left **unmatched** rather than pinned to one arbitrary id and
its partial spend; those names are listed in `dw_supplier_name_collision`
(warn checks `canonical_crosswalk_gaps` and `side_input_ambiguous_names`).
Unifying the ids in `references/supplier_master.csv` restores the match.
Only company ids are candidates: the `UNKNOWN` bucket (summary rows with no
supplier id keep the vendor's name, e.g. WW GRAINGER) and government /
placeholder ids (`DEPT…`, `UNIV…`, `…CFD`, `…SGP`) never count as a rival.
The rule also runs from the other side: 2+ distinct eProcure firms
(`cert_id`) whose names normalize alike stay unmatched too, listed in
`dw_side_input_name_collision` and counted by `side_input_ambiguous_names`.
The `UNKNOWN` bucket's `canonical_name` is always `Unknown supplier`.

**eProcure SB/DVBE certification integration:** a third optional side input,
Cal eProcure's certified-supplier registry (`data/eprocure.db`,
`src/eprocure.py`), is snapshotted to `bronze_eprocure` and matched to
`dim_supplier` by the same normalized-name mechanism as CMAS. Registry grain is
one row per **certification record** — a firm (`cert_id`) can hold several,
e.g. separate SB and DVBE certifications with different date ranges. It exposes
**`gold_eprocure_certification`** (every certification record + derived
`cert_small_business`/`cert_micro_business`/`cert_dvbe` flags + a
`matched_to_supplier` flag) and **`gold_supplier_certification`** (our canonical
suppliers holding a state certification — SCPRS spend beside authoritative
SB/micro/DVBE status, flags OR-ed across the firm's certification records).
The registry's contact PII and owner-demographic columns deliberately stay in
`eprocure.db`. Absent the file, the build is a clean no-op. See
`docs/EPROCURE.md`.

**eProcure CSCR events integration:** the same store's `events` table (CSCR
solicitations: live Posted + the recent Historical years `extract-events`
covers) is snapshotted to `bronze_eprocure_events`. Grain: one row per
`(department_name, event_id)` — bare event ids collide across departments, and
an event captured both live and completed resolves to the completed row in
**`gold_eprocure_event`**. Departments match to a `business_unit` by **exact
FI$Cal reference name** (only where the name maps to a single BU; DAAs and
superior courts stay NULL — ~2% of events). On top sit
**`gold_eprocure_posted_opportunity`** (solicitations open for bid as of the
last extraction, with bid close dates) and **`gold_eprocure_event_demand`**
(per department × bid-close year: solicitation volume and SB-only/DVBE-only
set-aside counts, plus `sb_or_dvbe_count` for set-asides open to either — the
demand-side companion to `gold_supplier_certification`). Caveats the marts
carry in their definitions: the set-aside flags are title-derived (re-derived
at ingest by `eprocure.set_aside_flags` from the "... ONLY" clause of the
event name), so counts are a floor, not a census; coverage is Posted + recent
Historical years, not the full archive; Posted rows have no published date by
source design. A pre-events `eprocure.db` (registry only) still builds cleanly.

## Contract change capture (history over time)
The bronze/silver/gold layers are a full-refresh snapshot of *current* state, so on
their own they can't show how a contract changed. **`dw_document_history`** fixes
that: an **append-only** table (never dropped on rebuild) that records a snapshot of
each document/version's tracked attributes — `version`, `grand_total`, `status`,
`start_date`/`end_date`, supplier, acquisition — with the batch and observation time.

`capture_document_history` runs each build and appends a snapshot only when a
document/version's signature is new, so:
- the first build **backfills** every version present now (immediate amendment
  history for the multi-version documents), and
- later builds append a row whenever a value, status, term, supplier, or version
  changes — building true change-over-time history as the daily job runs.

**Persistence across CI rebuilds.** The scheduled `warehouse-refresh.yml` rebuilds
`warehouse.db` from nothing every run, and `serve-export` drops the history from
the serve DB. So the table is carried run to run as its own file,
`warehouse-history.db`, in the warehouse dataset beside `warehouse-serve.db`:
1. `data_sync fetch-history` downloads it. **Fail-closed:** if the dataset can't be
   read, or holds the file but the download fails, the run stops; only a confirmed
   absence (first run) starts history fresh.
2. `warehouse build --seed-history data/warehouse-history.db` merges it in before
   capture, keeping the original `history_sk` / `observed_at` values, so the
   signature gate appends only what changed since the last run.
3. `warehouse export-history` writes the table alone back out — and **refuses** if
   the new history has fewer rows than the copy it was seeded from.
4. `data_sync publish --with-history` puts the serve DB and the history in **one**
   commit, refusing if this run didn't fetch the history or the dataset's copy
   changed since (blob-id check scoped to the history file, pinned to the checked
   HEAD as `parent_commit` so nothing can land in between). **When the build
   appended no history rows** (exported rows = rows fetched), the ~0.6 GB history
   upload is **skipped** and only the serve DB is published — every dataset
   revision is kept, so re-uploading an identical file each refresh is pure
   storage growth.

**One-time seed of older local history.** A laptop `warehouse.db` may hold
snapshots CI never saw (the July 2026 batches). `python -m src.data_sync
merge-history --dataset <warehouse dataset>` fetches the dataset copy (fail-closed),
merges the local rows in (local first, so their earlier observations keep the lower
keys; rows already present by document/version/signature are skipped), refuses if
the result would be smaller than the dataset copy, and prints the counts — a **dry
run**. Add `--apply` to publish the merged history alone as one compare-and-swap
commit; the next refresh seeds from it. Run it right after a refresh finishes: a
refresh that fetched the old copy refuses to overwrite the merged one (and the next
refresh then picks it up).

A serve rollback (`rollback-serve`) reverts only the serve DB; the history stays at
its newest copy — it is append-only observations, and a go-live failure says nothing
about them. Local builds keep using their own `warehouse.db`'s history (no seed), and
`warehouse-diff.yml` still deliberately builds from a clean `warehouse.db`.

Two marts derive from it:
- **`gold_contract_change_log`** — one row per observed transition (`v1 -> v3`,
  value delta + %, status change, term extension) with a readable `change_summary`.
- **`gold_contract_amendments`** — per contract: amendment count (= current version),
  snapshots captured, current value, value growth, and observation window.

## Control & data quality
- **`dw_batch`** — one row per build (batch id, start/finish, status, row counts).
- **`dw_dq_results`** — every check's outcome per batch, with a **severity**:
  - `error` (gates the build): no null document keys, unique document grain,
    document-grain parity (distinct bronze documents = `fact_document` rows,
    end to end), and fact→dimension referential integrity.
  - `warn` (informational): line-item reconciliation, negative grand totals
    (real credits/deobligations in the source data), `orphan_drill_rows`
    (drill lines/POs whose document left the summary — dropped from both
    facts), `summary_rows_without_attributes` (blank rows SCPRS lists with no
    version/date/value; kept), and the crosswalk / side-input name checks.

## PR output diff (`src/warehouse_diff.py`)
A change to `warehouse.py` (a mart, a grain, a join, an abbreviation) can shift the
gold **output** in ways a small-fixture unit test won't catch. The `warehouse-diff`
CI job (on PRs touching `warehouse.py`/`supplier_master.py`/`model.py`/
`references/*.csv`) builds the warehouse from the **same real operational data** on
both the PR code and the base code, and posts how gold changed as a PR comment:
added/removed objects, per-mart **column** changes, **row-count** deltas, and a
curated set of **headline metrics** (spend, supplier counts, CMAS coverage — all
queried through the logical `lv_`/`gold_` views). It is **informational, not a
gate** — warehouse output changes are often intended — but flags the shapes worth a
second look (removed objects/columns, row-count drops >2%) with ⚠️. Two steps:
`warehouse_diff snapshot` captures a build's contract to JSON; `warehouse_diff
report` renders the base→head markdown.

## Physical naming (abbreviation standard)
Physical columns on the gold `dim_*`/`fact_*` **tables** are abbreviated to a
governed standard from `references/abbreviations.csv` (a `term → abbreviation`
dictionary: `amount → amt`, `supplier → sup`, `date → dt`, `business_unit → bu`,
…). Applied token-by-token by `abbreviate()`, so `fact_document.grand_total`
becomes `grand_tot`, `dim_supplier.supplier_id` becomes `sup_id`, etc.

Analysts don't have to memorize the abbreviations — the marts stay friendly:
- For each gold table there is a **`lv_<table>` view** that aliases the abbreviated
  columns back to their logical names (`lv_dim_supplier.supplier_id`, …).
- All `gold_*` marts and the data-quality checks read those `lv_` views, so mart
  **output** column names are unchanged (`SELECT total_value FROM gold_supplier_master`
  still works).
- **`gold_data_dictionary`** records every `(table, logical_name, physical_name)`.

The abbreviation runs as a post-build rename pass (`_abbreviate_gold`), so adding a
term to the CSV and rebuilding is all it takes to restandardize.

## Keys, audit columns, and large-text typing
- **Surrogate keys.** Dimensions carry `*_key` surrogates; the fact and silver
  tables (and `dw_document_history`) each carry an integer surrogate PK too
  (`document_sk`/`line_sk`/`po_sk`/`history_sk`). These are distinct from the
  degenerate business key `document_bk` and the dimension FKs (`*_key`).
- **Audit columns.** Facts and silver tables carry `dw_batch_id` + `dw_loaded_at`
  (dimensions already carry `dw_loaded_at`; bronze carries `_batch_id`/`_loaded_at`/
  `_source`). Every row is traceable to the build that produced it.
- **Large text → CLOB.** Long free-text columns (`item_description`,
  `unspsc_description`) are declared `CLOB`. In SQLite that is TEXT affinity — full
  `LIKE`/sort behaviour, no storage change — and it ports cleanly to Oracle/Postgres.
  (`BLOB` is deliberately avoided: it is binary and would break text search/sort.)
  The `_finalize` helper stamps the surrogate key + audit columns and re-declares the
  CLOB columns while preserving every other column's numeric/text affinity.

## Design notes / best practices applied
- Separation of operational (`scprs.db`) and analytical (`warehouse.db`) stores.
- Immutable raw layer with lineage; transformations only move *forward* a layer.
- Explicit grain per table; current-version resolution for slowly-changing docs.
- Conformed dimensions, surrogate keys, degenerate dimensions, Unknown members.
- Idempotent full-refresh loads; batch control + severity-tiered data quality.
- **SCD note:** dimensions are currently Type 1 (overwrite on rebuild). The
  surrogate keys already decouple facts from natural keys, so upgrading
  `dim_supplier`/`dim_buyer` to Type 2 (history) is a localized change.
- **Change history:** while the star is a current-state snapshot, the append-only
  `dw_document_history` captures how each *contract* changes over time (see above),
  so amendments are not lost even though `silver_document` keeps only the current
  version.
