# Power BI over the SCPRS warehouse

The pipeline publishes a curated set of **BI-shaped mart CSVs** to the private
serve dataset alongside `warehouse-serve.db`, refreshed by the serve-refresh
CI workflow (every 4 hours, up to 6×/day). Power BI consumes them with
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
2. In Power BI Desktop, define the plumbing **once** — a `Token` parameter and
   two loader functions (Get Data → Blank Query → Advanced Editor, one query
   each, named exactly as shown). Every table is then a one-liner.
3. First connect: choose **Anonymous** when Power BI asks for credentials —
   authorization is carried by the explicit header, not the credential UI.

Query `Token` (a text parameter — token rotation = edit this one value):

```m
"hf_xxx" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
```

Query `LoadMart` (flat CSV marts):

```m
(Name as text) =>
let
    Url   = "https://huggingface.co/datasets/munther-hasan/scprs-warehouse-data/resolve/main/marts/" & Name & ".csv",
    Raw   = Web.Contents(Url, [Headers=[Authorization="Bearer " & Token]]),
    Csv   = Csv.Document(Raw, [Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),
    Promoted = Table.PromoteHeaders(Csv, [PromoteAllScalars=true])
in
    Promoted
```

(Don't name that step `Table` — M's `let` scope is recursive, so a local named
`Table` shadows the standard library and `Table.PromoteHeaders` becomes a
cyclic reference.)

Query `LoadStar` (Parquet star exports, see below):

```m
(Name as text) =>
Parquet.Document(
    Binary.Buffer(
        Web.Contents(
            "https://huggingface.co/datasets/munther-hasan/scprs-warehouse-data/resolve/main/marts/star/" & Name & ".parquet",
            [Headers=[Authorization="Bearer " & Token]]
        )
    )
)
```

Then each table is a New Blank Query with a single formula-bar line — rename
the query to the table name and Close & Apply loads them all in one pass:

```m
= LoadMart("department_fiscal_year_spend")
= LoadStar("fact_document")
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

Load all nine as one-liners over the `LoadStar` function from the setup
section (one blank query each, named after the table):

```m
= LoadStar("fact_document")
= LoadStar("fact_line")
= LoadStar("fact_associated_po")
= LoadStar("dim_supplier")
= LoadStar("dim_department")
= LoadStar("dim_date")
= LoadStar("dim_acquisition")
= LoadStar("dim_buyer")
= LoadStar("dim_unspsc")
```

The `Binary.Buffer` inside `LoadStar` is required: `Web.Contents` returns a
streamed binary, and `Parquet.Document` errors on streams ("cannot be used
with streamed binary values") because it must seek to the file-footer
metadata. Buffering reads the whole file into memory first — fine at these
sizes.

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

## Ready-made PBIP project (skip all of the above)

`powerbi/scprs-star.pbip` is a Power BI Project with the whole model
pre-wired: the `Token` parameter, `LoadMart`/`LoadStar` functions, the nine
star tables (typed columns, surrogate keys hidden), **all twelve flat mart
CSVs** (typed in-partition — no manual column typing), the relationships,
`dim_date` marked as the date table (`full_date` coerced string→date in its
partition), and starter measures that encode the grain rules
(`Total Contract Value` = document-grain `grand_total`; the line-amount
measure is labeled enriched-docs-only).

Supplier certification and CMAS status are modeled as a snowflake:
`gold_supplier_certification` and `gold_supplier_cmas` are one row per
canonical supplier, related from `dim_supplier.canonical_name` (many→one), so
their flags cross-filter every fact through the supplier dimension. For easy
slicers, `dim_supplier` carries calculated columns `sb_certified` /
`dvbe_certified` / `cmas_holder` ("Yes"/"No") derived via `RELATED()`. The
other marts are standalone tile tables (aggregates — don't relate them to the
star; they'd double-count).

Open it in Power BI Desktop (a 2024+ build — the model is stored as TMDL),
set `Token` under Transform data → Manage parameters, and Refresh.
`powerbi/.gitignore` keeps Desktop's local `.pbi/` caches out of git.

## Report layer (the SCPRS dashboard)

The report side of the PBIP implements the *SCPRS Power BI Build Spec* (v4, in
the claude.ai/design project alongside the dashboard mockup): a 1600×900
canvas, ten pages plus a hidden Notes page, and a `_Measures` table with the
full measure set (competitive/leveraged share, SB/DVBE goals, HHI, CMAS
expiry, amendment growth, eProcure event counts). Three facts that came from
profiling, not the spec draft: `competitive_flag` values are
`Competitive`/`Non-Competitive`/`Other` (title case);
`gold_market_concentration.top_supplier_pct` is 0–100 (the `[Top Supplier %]`
measure divides by 100 so it formats as a percentage); and neither
`purchase_document` nor `department_name` is unique in the star, so the four
mart relationships key on `business_unit` and on a calculated
`business_unit|purchase_document` composite (matching the hidden
`fact_document.document_bk`) rather than the spec's draft columns.

`python powerbi/build_report.py` generates the whole report file: the
**chrome** (sidebar — its 1px dividers and 3px active-page bar are below the
12px minimum Desktop's UI allows on shapes, hence the script — nav buttons,
header, the three synced slicers, the Filter State scope card, the registered
`industry-theme.json`) **and every page's content visuals** — KPI cards,
charts, tables and the fiscal-year matrix, bound to the model at the spec's §6
coordinates, including the Top-N filters on the ranking bars and the Pareto.
Visual names mark ownership: `chrome_*` is always regenerated; `gen_*` content
and anything hand-built survive `--merge` (a visual you edited in Desktop wins
over its regenerated twin by name); `--force` discards edits and rebuilds
everything. Install **Barlow** and **Barlow Condensed** on every author and
viewer machine or the theme silently falls back to Segoe UI.

Still manual in Desktop, per the spec's own "cannot do natively" list plus
format-pane-only settings: page 10's drill-through field list
(`canonical_name`, `department_name`, `acquisition_type`, `fiscal_year`), the
"Clear all" bookmark button, constant lines (SB 25% / DVBE 3% goals, portfolio
average), the fiscal-year matrix heat shading (Cell elements → Background
gradient `#F2F2F3 → #94BCE3`), conditional formats on expiry/close dates, and
the report-page tooltip.

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
