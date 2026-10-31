# Vendor web app — static JSON export

`src/app_export.py` writes the data behind the planned vendor/small-business web
app: a static site that reads precomputed JSON, so the front end never sums,
dedupes or joins. The field-level spec (schema v1) is the "Data contract" tab of
the product brief (Claude Doc, linked from the PR that added this module).

```bash
python -m src.warehouse serve-export          # the export reads the slim serve DB
python -m src.app_export                      # -> data/app/
python -m src.app_export --today 2026-07-25   # pin "open" solicitations to a snapshot date
```

| File | Contents |
| --- | --- |
| `meta.json` | freshness dates (`contracts_as_of`, `opportunities_as_of`, …) and coverage counts the UI shows beside totals; `coverage.note` (one sentence to show beside every total), `coverage.non_procurement_value` (class X, FY2021-22 on), `explore.beta` |
| `opportunities.json` | posted solicitations closing on/after the export date, soonest first |
| `departments/index.json` | one row per business unit, loaded first by value |
| `departments/{bu}.json` | spend by fiscal year, acquisition mix, top canonical suppliers, canonical HHI, solicitation history, open solicitations. Since 2026-10-31 every total, share and ranking is **procurement only** (`gold_procurement`: award class X — interagency, assistance, leases, reviewed pass-throughs — excluded) and `non_procurement` reports what was set aside; `competitive_share_pct` / `leveraged_share_pct` / `noncompetitive_share_pct` are classes C / L / N over C+L+N (State Contracting Manual codes; Fair and Reasonable is N); each `acquisition_mix` row carries `award_class`, and its `competitive_flag` is derived from it (`Competitive` / `Leveraged` / `Non-Competitive` / `Other`). Supplier pages and insight pages are procurement only as well. |
| `suppliers/index.json` | one row per canonical supplier (search) |
| `suppliers/shard-{NN}.json` | supplier pages keyed by `canonical_id`; `NN` = its last two digits, `xx` for non-numeric ids |
| `insights/overview.json` | the dashboard home: headline counts, spend by fiscal year (with `department_count` reporting each year), top departments and companies, acquisition / size / category mixes, solicitations closing soon |
| `insights/{acquisition,size,category}/{key}.json` | one page per vector value: totals, trend, top departments and suppliers, concentration, plus a cross-mix (size mix, acquisition mix, or top UNSPSC commodities) |
| `search.json` | small index (~60 KB) over departments (with aliases) and every vector value; suppliers stay in the lazy `suppliers/index.json` |
| `cmas/agreements.json` | the CMAS tab: every CMAS agreement (~2,500, ~1.8 MB — lazy-load on the tab), each with derived `status` (active/expired vs the export date), `enterprise_type` + `small_business`/`dvbe`, `product_service_codes`, code-family `categories`, authorized `brands` (from `BRAND-*` codes), city/state, and a `supplier` link (`canonical_id`, `shard`) when the holder also has SCPRS contracts (~40%); `facets` gives value/count lists for status, enterprise type, state, category and brand. Filtering and search are client-side. Contact name/email and company website are excluded (public slice). Absent when the build had no `cmas.db`. `meta.cmas_as_of` is the extraction date (null for a store extracted before 2026-10-31). |
| `explore/cube.json` | **Explore (beta)**: a columnar cross-filter cube from FY2021-22 — one row per fiscal year × department × acquisition type × size band × award family (`columns.*` arrays line up by index; `count`, `value` in dollars), with `dimensions` holding the labels. Each family carries its `award_class`: C competitive, L leveraged agreement, N non-competitive, X not procurement (interagency, subvention, leases, reviewed pass-throughs), U unknown — from the governed `references/award_methods.csv` (SCM code lists 14-01/15-01/16-01). The page excludes X from totals by default. ~460 KB. |
| `explore/contracts/{bu}.json` | Explore's contract list for one loaded department: every contract of $100K or more (`contracts_listed_from`), largest first, with `title` (SCPRS first-item title), canonical company + `shard` (link to the supplier page), value, start date, fiscal year, acquisition type, raw `award_method`, `scm_code`, `family` (index into the cube's families), status, `line_count`, and `flag` for reviewed outliers (`references/contract_flags.csv`). Load only for departments in view; up to ~4 MB. |

Insight vectors:

- **Acquisition type** rolls any glued sub-type (`NON-IT Services_Expert Witnesses`)
  up to the base type before `_` — a guard; silver splits these since 2026-10.
- **Contract size** buckets `grand_total` (`SIZE_BUCKETS`); `$0` and credits get
  their own `adjustment` bucket so they don't read as small buys.
- **Product category** maps line-item UNSPSC codes to ~15 plain-language
  categories via `references/product_categories.csv` (longest prefix wins, so
  `8111` IT services beats `81` engineering). It covers only itemized contracts:
  `basis: "line_items"`, and the UI must say so.
- **Department aliases** (Caltrans, CHP, DMV, …) come from
  `references/department_aliases.csv`; add a row when a search misses.

Rules the export enforces (don't move them into the front end):

- **Canonical suppliers only.** Shares and HHI are recomputed from `gold_document`
  by `canonical_id` (the export's own computation; `gold_supplier_share` /
  `gold_market_concentration` are canonical too since 2026-10, but the app
  doesn't depend on them).
- **Null, not zero.** Unloaded departments still get a page (eProcure history and
  open solicitations cover every department) with `loaded: false` and null spend blocks.
- **`supplier_kind`** labels interagency counterparties (`DEPT…`/`UNIV…` →
  `government`) and redacted/pooled payees (`…CFD`, `…SGP`, `UNKNOWN` →
  `placeholder`) so the UI doesn't present them as competitors; totals still include them.
- **`partial` on every fiscal-year row** (department pages, the overview trend,
  supplier pages): true when the data window doesn't span the whole Jul–Jun
  year, with coverage ending at the export date — so the year in progress and
  later years (contracts entered ahead of their start date) are always partial.
  Department rows use the department's own window; overview and supplier rows
  use the warehouse's.
- **`cmas_holder`** needs a CMAS agreement still in term on the export date.
- **Public slice.** CMAS contact name/email/website are never selected.
- The output directory is rebuilt via a staging dir and swapped in, so a department
  or supplier that leaves the warehouse can't leave a stale page behind.

## Publishing

`warehouse-refresh.yml` runs the export and `python -m src.data_sync publish-app`
after go-live verifies the build, so only a verified build reaches the public
slice. One commit per publish; files the export no longer writes are deleted.
The step is dormant until set up:

1. Create the **public** dataset in the HF UI (no pipeline token can create repos,
   and `publish-app` deliberately never creates one).
2. Mint a fine-grained token with write on that dataset only; add it as the
   `HF_APP_TOKEN` Actions secret.
3. Set the `APP_DATASET` Actions variable to the dataset id.

Not wired yet: the static Space that serves the site.
