---
name: data-analysis
description: Answer analytical questions about California procurement from the warehouse — correct grain, canonical suppliers, coverage caveats, money semantics. Use when querying spend/contracts/suppliers for a human answer, building a report, or sanity-checking numbers before presenting them.
---

# Data analysis: answering procurement questions without lying

## Query the right layer

- Analytical questions → **gold marts through `src/warehouse_query.py`**
  (or the MCP tools) — never raw operational tables.
- Start with `gold_data_dictionary` / `list_marts` to pick the mart; use
  `distinct_values` when a filter value is uncertain (category questions:
  check whether the user means `acquisition_type` or UNSPSC — models
  routinely pick the wrong one).

## The traps that produce confidently wrong numbers

1. **Version grain**: anything computed off detail tables must resolve to the
   current document version or it double-counts amendments. Gold already does
   this; ad-hoc silver/operational queries must do it manually.
2. **Supplier identity**: one company = several `supplier_id`s (NORTH RIDGE
   CONSULTING has 2, BETA ALPHA PSI 4). Vendor rankings/rollups use
   `gold_canonical_supplier_spend` / `gold_supplier_master`.
3. **Coverage**: line-item detail exists only for enriched days. Before
   claiming "total line-item X", check enrichment coverage for the relevant
   BU/date range (`details_progress`, health coverage findings) and caveat
   accordingly. Summary-level (`grand_total`) figures are complete; line-level
   figures are only as complete as enrichment.
4. **Money semantics**: `grand_total` = merchandise + freight/tax/misc; line
   sums reconcile to merchandise, not grand total. Dollars are **nominal** —
   multi-year growth statements should note inflation (no deflator in the
   warehouse yet).
5. **Fiscal framing**: California FY is July–June; "2024 spending" is
   ambiguous — state which framing you used.
6. **Snapshot age**: the Spaces serve a boot-time snapshot; local warehouse.db
   is whatever was last built locally. State the data's as-of point when it
   matters; `gold_*` batch info / `dw_batch` has the build time.

## Change-over-time questions

"How did this contract change" → `gold_contract_change_log` /
`gold_contract_amendments` (append-only history). Silver only holds current
state; using it for change questions silently returns nothing.

## Presenting

Verify any headline number two ways (e.g. mart vs direct fact aggregation)
before presenting. Charts: load the global `dataviz` skill first; reports via
the MCP `generate_report` tool conventions.
