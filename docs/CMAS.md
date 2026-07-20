# CMAS extractor (`src/cmas.py`)

Pulls California's **CMAS** (California Multiple Award Schedules) contract data
from the public search site
[`cmassearch-prod.apps.dgs.ca.gov`](https://cmassearch-prod.apps.dgs.ca.gov)
into a standalone store — its own `data/cmas.db` + one CSV per table. The
extractor is a distinct data source (CMAS master-agreement contractors), not
another SCPRS stage. The warehouse then folds it in as an **optional side input**
and joins it to the supplier dimension (see [Warehouse integration](#warehouse-integration)).

## Why it isn't a normal scrape

The site is not an HTML app — it's an **embedded Power BI report** (US Gov
cloud). There's no official public API, but the report is served through Power
BI's *anonymous embed* flow, and the underlying model is fully queryable over the
same protocol the report's own visuals use. So instead of clicking each record's
drill-down, the extractor queries whole tables — every record already carries all
the per-record fields the UI shows one row at a time.

## The flow (all anonymous — no login, no secret)

1. `GET` the app page → a short-lived **EmbedToken** + ReportId + Power BI
   cluster URL (baked into the page's `ReportEmbedData` JS object).
2. `GET .../explore/reports/{id}/modelsAndExploration` → an **MWCToken**, the
   **capacity query endpoint** (`…pbidedicated…/…/QueryExecutionService/…/query`),
   and the model id. This is the read-only session the report opens on load.
3. `GET .../conceptualschema` → entities + columns, so extraction is driven by
   the **live schema**, not a frozen list. Measures (e.g. `Last Date Refreshed`)
   are skipped — only real columns are row-queryable.
4. `POST` a Power BI *semantic query* (all columns, large page window) to the
   capacity endpoint with the MWCToken → rows in Power BI's compressed **DSR**
   format, paged via `RestartTokens`.

`_decode_dsr` expands the DSR: value-dictionary columns, the row-to-row repeat
bitmask `R`, the null bitmask `Ø`, the inline (uncompressed) small-result form,
and epoch-ms datetimes. This decoder is the load-bearing, tricky part and is what
`tests/test_cmas.py` locks down (offline, from real captured shapes).

**Transport quirk:** the Power BI Gov front end drops some HTTP clients' POSTs at
the TLS layer (`httpx` is refused; `requests`/urllib3 is accepted). GETs use
`httpx`; the query POST uses `requests` — both already project dependencies. No
browser or extra tooling is required.

## Commands

```bash
python -m src.cmas schema                 # print the live model schema
python -m src.cmas extract                # all entities -> data/cmas.db + CSVs
python -m src.cmas extract --entity Approved_Applications
```

## What you get (7 tables; counts as of 2026-07-20)

| Table | Rows | Notes |
|---|---|---|
| `Approved_Applications` | ~2,400 | The main table — 45 cols; contractor, agreement number, base schedule, addresses, contacts, product/service codes, SB/DVBE, term dates. Contains the full per-record drill-down. |
| `Approve_App_Product_and_Service_Codes` | ~22,700 | Agreement ↔ product/service code join. |
| `CMAS_Product_and_Service_Codes` | ~2,000 | Product/service code lookup (+ green flag). |
| `Company_Profile` | ~3,600 | Supplier company profiles. |
| `Contact_User_Info` | ~3,300 | Contact records. |
| `SB` / `DVBE` | 1 each | Small-Business / Disabled-Veteran flag label rows. |

Tokens are short-lived (~10 min); a full extract completes well within one
session, so no token refresh is needed. Output is a full idempotent refresh
(drop + recreate each table) — rerun any time for fresh data.

## Warehouse integration

`warehouse.py` folds CMAS in as an **optional side input**, exactly like
`supplier_enrichment.db`: if `data/cmas.db` is present it's ingested; if absent the
build is a clean no-op (empty tables, no failure). No CMAS extract is required to
build the warehouse.

- **`bronze_cmas`** — the CMAS agreements landed at build time (`_ingest_cmas` in
  `build_bronze`), a curated column subset plus a `supplier_norm` (normalized
  supplier name) computed at ingest.
- **Name matching** — CMAS names are mixed-case with punctuation
  (`HCI Systems, Inc.`); warehouse supplier names are upper-case, no punctuation
  (`HCI SYSTEMS INC`). A plain `UPPER()` join (what the web-enrichment mart uses)
  matches only ~8% of CMAS suppliers; reusing `supplier_master.normalize_name`
  (strip punctuation + legal suffixes like Inc/LLC) lifts that to ~22% of distinct
  names. `_resolve_cmas_suppliers` (a gold step, after `dim_supplier` exists) does
  this matching **in Python** and stamps each agreement's
  `matched_canonical_id`/`matched_canonical_name`, so the gold marts stay plain
  equality joins. The join is to the **canonical** supplier, so split registrations
  resolve to one entity.
- **`gold_cmas_agreement`** — every CMAS agreement with its `matched_to_supplier`
  flag (NULL match = a statewide CMAS holder we have no SCPRS spend record for).
- **`gold_supplier_cmas`** — the payoff: our canonical suppliers that *also* hold a
  CMAS master agreement, with their SCPRS spend (`scprs_total_value`,
  `document_count`) beside their CMAS terms, SB/DVBE status, base-schedule count,
  and agreement numbers. Use it to see which vendors you already spend with are on
  a statewide CMAS vehicle.

The overlap is genuinely partial (~330 of our canonical suppliers match, ~29% of
CMAS agreements) — expected, since CMAS is statewide while the SCPRS spend data
covers a priority subset of departments. Both marts flow into the slim serve DB
(materialized, since they read `bronze_cmas`), so the MCP server and web app serve
them automatically.

## Device-free refresh (CI)

CMAS refreshes itself in GitHub Actions — no laptop, like the rest of Wave 2.
`.github/workflows/cmas-refresh.yml` runs daily (`37 6 * * *`): `src.cmas extract`
(pure HTTP, no browser) → a non-empty sanity check → `data_sync publish-cmas`,
which uploads `cmas.db` **alongside `scprs.db`** in the operational dataset
(`munther-hasan/scprs-operational-db`). It shares the `scprs-operational-writer`
concurrency group with the enrich workflow, so two commits never race the dataset,
and only a non-empty extract is published (upload-on-success — a run blocked by
cloud IPs never overwrites good data).

The enrich workflow's `data_sync fetch-operational` then pulls `cmas.db` (best
effort, alongside `scprs.db` and `supplier_enrichment.db`) before `warehouse
build`, so the CI-built gold — and the served marts — carry real CMAS data. Absent
the file (before the first successful refresh), the build skips CMAS cleanly.
