---
name: warehouse-modeling
description: Fold data into the medallion warehouse (bronze/silver/gold), add or change warehouse tables, side-input joins, DQ checks, or history capture. Use when touching src/warehouse.py, adding a new side input to the build, or changing grain/joins — the traps here silently corrupt analytics.
---

# Data engineering: warehouse rules that keep the numbers honest

Read `docs/WAREHOUSE.md` for the full layer/grain spec. The load-bearing rules:

## Grain — the #1 double-count trap

SCPRS documents exist at `(document, version)` grain. Silver and every query
over `document_details`/`document_lines`/`document_pos` must resolve to the
**current version** (max version present) before aggregating. Any new query or
mart that forgets this double-counts re-drilled documents.

## Layer discipline

- `build_all` = full idempotent refresh bronze → silver → gold in one `dw_batch`,
  source ATTACHed **read-only**. Operational (`scprs.db`) and analytical
  (`warehouse.db`) stores never mix.
- **`dw_document_history` is append-only and NEVER dropped** — it's the only
  place contract change-over-time survives full refreshes. Signature-gated
  appends keep rebuilds idempotent. Amendments/value-growth queries go through
  `gold_contract_change_log`, never silver.
- DQ checks are severity-tiered: `error` gates the build (non-zero exit),
  `warn` informs. New tables get checks in the same pass.

## Side-input folding (the extension mechanism)

New sources fold in as **optional side inputs**: skip-if-absent, so a build
without the file still succeeds (empty marts, not errors). Join strategies, in
order of reliability:
1. **Department/BU code** via `references/departments.csv`
2. **Normalized supplier name** (the CMAS pattern) — which lands on the
   canonical entity; see supplier identity below
3. **Month** via `dim_date` (economic indicators)

## Gold conventions

- Physical `dim_*`/`fact_*` columns are abbreviated post-build from
  `references/abbreviations.csv`; marts and DQ reference the per-table `lv_*`
  logical views, **never physical names**. Adding a gold column: just rebuild;
  add new words to the CSV. `gold_data_dictionary` records the mapping.
- **Supplier identity is many-to-one**: one company holds several
  `supplier_id` registrations. Vendor rollups must use the canonical marts
  (`gold_canonical_supplier_spend`) stamped from
  `references/supplier_master.csv` — per-id marts double-count split vendors.

## SQL safety invariant

`warehouse.py` is exempt from S608 because its dynamic SQL uses **internal
constants only** with parameterized values. Never interpolate user or source
data into DDL/DML strings — keep that invariant true or the exemption becomes
a hole.

## Verification

`pytest tests/test_warehouse.py`; `python -m src.warehouse dq`. PRs touching
warehouse-shaping code get an automatic **warehouse-diff** comment (both builds
on real data) — read it; unexpected mart deltas are the review signal.
