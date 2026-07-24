# eProcure registry extractor (`src/eprocure.py`)

Pulls Cal eProcure's **SB/DVBE certified-supplier registry** from the public
search on [`caleprocure.ca.gov`](https://caleprocure.ca.gov) into a standalone
store — its own `data/eprocure.db` + a CSV. The extractor is a distinct data
source (state certifications: Micro/Small Business, DVBE, NVSA, NP), not
another SCPRS stage. A follow-up folds it into the warehouse as an **optional
side input** joined to suppliers by normalized name, like CMAS.

## Recon map (2026-07-23) — why it's a browser scrape

caleprocure.ca.gov is an **InFlight NLX** overlay on the same PeopleSoft
instance SCPRS runs on (`suppliers.fiscal.ca.gov`, portal `psfpd1/SUPPLIER/ERP`).
The overlay routes were probed and are not drivable without a browser:

| Route | Result |
|---|---|
| `caleprocure.ca.gov` page URLs (`/pages/...`) | HTML shells only; all data is fetched client-side. Curl's default UA gets **403** (WAF) — a browser UA is required even for shells. |
| InFlight relay (`/pages/ps-relay.aspx`, `/nlx3/psc/...`) | GET → empty shell / WebLogic 404. The relay's JSON capture protocol exists (`app.min.js`) but is session-bound and obfuscated. |
| Raw PeopleSoft ICAJAX POSTs | **Black-holed by the WAF at the TLS layer** for non-browser clients (curl: schannel reset; httpx: read-timeout). GETs pass, POSTs don't. |
| **Headless Playwright on `suppliers.fiscal.ca.gov`** | **Works, anonymously** — same mechanism as `src/scprs.py`. This is what the extractor uses. |

WAF etiquette: pace requests, reuse one browser context, and expect 403s if
you burst direct-link URLs (`...GBL?...&BUSINESS_UNIT=X&AUC_ID=Y`) headlessly.

## The flow (all anonymous — no login, no secret)

Component: `ZZ_PO.ZZ_PUBSRCH.GBL` ("Search Certified Firms").

1. `GET` the component → the search form. Six certification-type checkboxes
   (`ZZ_PUBSRCH1_WRK_FLAG1$0..5`): MB, SB, SB-PW, DVBE, NVSA, NP.
2. Check **all six**, verify each actually committed (silently-unchecked boxes
   would narrow the search — same failure family as SCPRS's date fields), then
   search. The result banner (class `PSGRIDCOUNTER`) reads literally
   `"1-10 of 21450"` — **not** the SCPRS grid's `"1 to 200 of 206"`; formats
   are verified against live text, never assumed (#49).
3. Click **Download-to-file** (`ZZ_PUBSRCH1_WRK_DOWNLOAD_TO_FILE`) → a
   PeopleSoft ".xls" that is really an HTML table
   (`Certification_Information_*.xls`, ~27 MB), cleaned at load time.

**The export is trustworthy** — unlike SCPRS's "Download Detail Information"
(which drops line-item dollars and is deliberately unused), this one was
reconciled field-by-field against the site's own result grid during recon
(10/10 rows, 8 fields each). It also carries *more* than the grid shows:
UNSPSC codes, keywords, NAICS, service-area counties, license classes,
industry types, and demographic fields.

## Grain and completeness

One row per **certification track**: a firm holding both SB(Micro) and DVBE
appears once per track — same `Certification ID`, different type and dates
(22,558 rows / 21,446 firms at recon). The banner counts distinct firms, so
completeness is judged on **unique Certification IDs vs the banner total**
with a 1% tolerance for live churn (recon saw a delta of 4 in 21,450);
anything short raises instead of writing — a partial export must never look
like a finished run.

## Commands

```bash
python -m src.eprocure extract-registry           # -> data/eprocure.db + CSV
python -m src.eprocure extract-registry --show    # headed browser (debug)
```

`data/eprocure.db` holds `registry` (32 export columns + `normalized_name`,
computed with `supplier_master.normalize_name` so certifications attach to the
canonical vendor entity downstream) and `extract_meta` (extraction timestamp,
banner total, row/firm counts). Full idempotent refresh — rerun any time.

## CI refresh

`.github/workflows/eprocure-refresh.yml` (weekly cron, offset from the enrich
and CMAS slots; shares the `scprs-operational-writer` concurrency group)
extracts and publishes `eprocure.db` to the private operational dataset via
`python -m src.data_sync publish-eprocure`. Gates, in order:

1. **In-extractor completeness check** — unique firms ≥ 99% of the site's own
   banner total, else the run fails before writing.
2. **Shrink gate** — the new store must hold ≥ 90% of the previously
   *published* rows (first publish passes on > 0), catching a search that
   silently covered a subset.
3. **Upload-on-success** — publish only runs when both gates pass.

Failures open a triage issue (`src/triage.py` hints under
"eProcure registry refresh").

## Follow-ups (tracked, not in this change)

- **Warehouse fold (PR B)**: optional side input like CMAS — skipped when
  `data/eprocure.db` is absent; cert flags joined by `normalized_name`.
- **CSCR events archive (PR C)**: the other eProcure surface
  (`AUC_MANAGE_BIDS.AUC_RESP_INQ_AUC.GBL`) — Historical years 2015–2026,
  ~5,610 events in 2025 alone, 50/page `PSGRIDCOUNTER` grid; row grain is
  BU + event id + name + format/type + dates + buyer contact. **Gated** on
  first verifying whether event details carry award/result data (the details
  drill opens a popup; unverified at recon).
