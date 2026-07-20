# CMAS extractor (`src/cmas.py`)

Pulls California's **CMAS** (California Multiple Award Schedules) contract data
from the public search site
[`cmassearch-prod.apps.dgs.ca.gov`](https://cmassearch-prod.apps.dgs.ca.gov)
into a standalone store — deliberately **separate** from the SCPRS pipeline
(its own `data/cmas.db` + one CSV per table). This is a distinct data source
(CMAS master-agreement contractors), not another SCPRS stage.

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
