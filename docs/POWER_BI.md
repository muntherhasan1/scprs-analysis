# Power BI over the SCPRS warehouse

The pipeline publishes a curated set of **BI-shaped mart CSVs** to the private
serve dataset alongside `warehouse-serve.db`, refreshed by the same CI cycle
(every successful enrich run, i.e. up to 8×/day). Power BI consumes them with
the built-in Web connector — no gateway, no ODBC, no server.

```
warehouse serve-export           # writes data/warehouse-serve.db + data/marts/*.csv
data_sync publish                # uploads both; CSVs land under marts/ in the dataset
```

The export list lives in `warehouse._MART_CSV_EXPORTS`: dashboard-shaped gold
marts (monthly spend, canonical supplier spend, acquisition mix, market
concentration, SB/DVBE certification, CMAS, open solicitations, contract
amendments) plus two export-only aggregates over `gold_document`
(`department_fiscal_year_spend`, `supplier_fiscal_year_spend`) — document
grain stays out of the feed (~1.5M rows of churn per publish); drill-through
belongs to the MCP/NL front ends.

## One-time setup

1. **Mint a token** (huggingface.co/settings/tokens): fine-grained, **read**
   on `munther-hasan/scprs-warehouse-data` only. Don't reuse a write token.
2. In Power BI Desktop: **Get Data → Blank Query → Advanced Editor**, paste
   the M below, one query per mart (rename the query to the mart name).
3. First connect: choose **Anonymous** when Power BI asks for credentials —
   authorization is carried by the explicit header, not the credential UI.

```m
let
    Token = "hf_xxx",   // read-scoped; store as a Power BI parameter
    Mart  = "department_fiscal_year_spend",
    Url   = "https://huggingface.co/datasets/munther-hasan/scprs-warehouse-data/resolve/main/marts/" & Mart & ".csv",
    Raw   = Web.Contents(Url, [Headers=[Authorization="Bearer " & Token]]),
    Csv   = Csv.Document(Raw, [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Table = Table.PromoteHeaders(Csv, [PromoteAllScalars=true])
in
    Table
```

Then set column types in the query editor (Power BI infers text for
everything from CSV): money columns → Fixed decimal, `fiscal_year` → Whole
number, dates → Date.

## Refresh

- **Desktop:** Refresh re-downloads the CSVs — data is as fresh as the last
  CI publish.
- **Power BI Service (scheduled refresh):** works with the same M because the
  token travels in `Web.Contents` headers. Set the data source credential to
  Anonymous and "Skip test connection" if prompted. Rotating the HF token
  means updating the `Token` parameter, nothing else.

## Star schema (relationships + drill-through)

For a properly modeled dashboard — slicers that cross-filter everything, and
drill-through from any tile to document detail — load the **star exports**
under `marts/star/` instead of (or alongside) the flat CSVs. They are the
warehouse's Kimball star via the `lv_*` logical views, as Parquet:

`fact_document` (~1.5M rows), `fact_line`, `fact_associated_po`,
`dim_supplier`, `dim_department`, `dim_date`, `dim_acquisition`, `dim_buyer`,
`dim_unspsc`.

```m
let
    Token = "hf_xxx",   // same read-scoped token
    Name  = "fact_document",
    Url   = "https://huggingface.co/datasets/munther-hasan/scprs-warehouse-data/resolve/main/marts/star/" & Name & ".parquet",
    Table = Parquet.Document(Binary.Buffer(Web.Contents(Url, [Headers=[Authorization="Bearer " & Token]])))
in
    Table
```

`Binary.Buffer` is required: `Web.Contents` returns a streamed binary, and
`Parquet.Document` errors on streams ("cannot be used with streamed binary
values") because it must seek to the file-footer metadata. Buffering reads the
whole file into memory first — fine at these sizes.

Wire the relationships in Model view (all many-to-one, single direction,
fact side → dim side):

| fact column | dim table.column |
|---|---|
| `fact_document.supplier_key` | `dim_supplier.supplier_key` |
| `fact_document.dept_key` | `dim_department.dept_key` |
| `fact_document.acq_key` | `dim_acquisition.acq_key` |
| `fact_document.buyer_key` | `dim_buyer.buyer_key` |
| `fact_document.start_date_key` | `dim_date.date_key` |
| `fact_line.*_key` | same dims, plus `fact_line.unspsc_key` → `dim_unspsc.unspsc_key` |
| `fact_associated_po.dept_key` / `start_date_key` | `dim_department` / `dim_date` |

Mark `dim_date` as the model's date table (`full_date`); use `fiscal_year` /
`fiscal_quarter` from it for CA fiscal framing. Measures live on the facts:
`SUM(grand_total)` (document grain, complete), `SUM(line_amount)` (line grain,
**enriched documents only** — never use it for totals). For drill-through,
build a "Document detail" page filtering `fact_document` (carry
`purchase_document`, `status`, `grand_total`, `line_count`) and add
drill-through fields for supplier/department/acquisition — the flat CSV tiles
and the star pages share dims, so right-click drill-through from any summary
visual lands on the underlying documents.

Two grain rules the model must respect: `fact_document` is one row per
document **current version** (no version double-counting), and vendor slicers
should use `dim_supplier.canonical_name` (one company can hold several
`supplier_id` registrations — slice on canonical, show `supplier_name` in
detail tables).

## Modeling notes (the usual traps)

- Vendor rollups: use `gold_canonical_supplier_spend` /
  `supplier_fiscal_year_spend` (canonical names) — never sum per-`supplier_id`
  marts, they double-count re-registered vendors.
- `total_value` sums are **contract grand totals booked to the start-date's
  fiscal year**, not annual outlays — label axes accordingly.
- `gold_contract_amendments` covers only documents captured at 2+ versions
  (sparse; deepest for BU 8660) — caveat any "fastest-growing contracts" tile.
- Coverage: summaries are complete for the loaded departments;
  **line-item-derived** figures are not in this feed at all (by design).

## Adding a mart to the feed

Add one entry to `warehouse._MART_CSV_EXPORTS` (mart name → SELECT) and
rebuild; `test_export_mart_csvs_from_serve_db` gates that every entry runs
against the serve DB, so a renamed gold column breaks CI, not your dashboard.
