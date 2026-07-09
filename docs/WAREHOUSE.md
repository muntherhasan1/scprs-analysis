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
`dim_date` (date spine + Unknown member), `dim_department`, `dim_supplier`,
`dim_buyer`, `dim_acquisition`, `dim_unspsc`.

**Facts:**
| Fact | Grain | Key measures |
|---|---|---|
| `fact_document` | one purchase document (current version) | merchandise, freight/tax, grand_total, line_count, associated_po_count |
| `fact_line` | one document line item (enriched docs) | quantity, unit_price, line_amount |
| `fact_associated_po` | one associated PO transaction (enriched) | po_total |

`document_bk` is a degenerate dimension (the natural document key) carried on the
facts. FKs use COALESCEd naturals so every fact row resolves to a real dimension
member.

**Marts (views):** `gold_supplier_spend`, `gold_monthly_spend`,
`gold_acquisition_spend`, `gold_unspsc_spend`, `gold_contract_vs_standalone`.

## Control & data quality
- **`dw_batch`** — one row per build (batch id, start/finish, status, row counts).
- **`dw_dq_results`** — every check's outcome per batch, with a **severity**:
  - `error` (gates the build): no null document keys, unique document grain,
    document-grain parity vs bronze, and fact→dimension referential integrity.
  - `warn` (informational): line-item reconciliation, and negative grand totals
    (real credits/deobligations in the source data).

## Design notes / best practices applied
- Separation of operational (`scprs.db`) and analytical (`warehouse.db`) stores.
- Immutable raw layer with lineage; transformations only move *forward* a layer.
- Explicit grain per table; current-version resolution for slowly-changing docs.
- Conformed dimensions, surrogate keys, degenerate dimensions, Unknown members.
- Idempotent full-refresh loads; batch control + severity-tiered data quality.
- **SCD note:** dimensions are currently Type 1 (overwrite on rebuild). The
  surrogate keys already decouple facts from natural keys, so upgrading
  `dim_supplier`/`dim_buyer` to Type 2 (history) is a localized change.
