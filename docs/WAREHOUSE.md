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
  be drilled at several versions), then de-duplicated.
- **Conforming.** Amounts/dates cast to real types; the combined acquisition
  string is split into `acquisition_type` / `acquisition_sub_type`; a
  `competitive_flag` is derived from the method.
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
taxonomy + department + `start_date`/`calendar_year`/`fiscal_year`; the primary
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
- `gold_supplier_share` — supplier share of each department's spend.
- `gold_market_concentration` — HHI + top-supplier share per dept × acquisition type.
- `gold_price_benchmark` — unit-price spread per UNSPSC where 2+ suppliers compete.
- `gold_supplier_unspsc_profile` / `gold_supplier_specialization` — what each
  vendor supplies and how specialized.
- `gold_supplier_acquisition_profile` — broad (all-document) category footprint.

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

Two marts derive from it:
- **`gold_contract_change_log`** — one row per observed transition (`v1 -> v3`,
  value delta + %, status change, term extension) with a readable `change_summary`.
- **`gold_contract_amendments`** — per contract: amendment count (= current version),
  snapshots captured, current value, value growth, and observation window.

## Control & data quality
- **`dw_batch`** — one row per build (batch id, start/finish, status, row counts).
- **`dw_dq_results`** — every check's outcome per batch, with a **severity**:
  - `error` (gates the build): no null document keys, unique document grain,
    document-grain parity vs bronze, and fact→dimension referential integrity.
  - `warn` (informational): line-item reconciliation, and negative grand totals
    (real credits/deobligations in the source data).

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
