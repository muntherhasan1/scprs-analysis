# SCPRS scraping notes

Reverse-engineering findings for the California FI$Cal SCPRS search:
`https://suppliers.fiscal.ca.gov/psc/psfpd1/SUPPLIER/ERP/c/ZZ_PO.ZZ_SCPRS1_CMP.GBL`

## Access
- **Public** — no login. It is a stateful PeopleSoft component (Oracle),
  not a static page. Data is refreshed every 24 hours.

## Search parameters (what you asked for)
| Field on page | HTML id | Notes |
|---|---|---|
| Department | `ZZ_SCPRS_SP_WRK_BUSINESS_UNIT` | 4-digit business-unit code; see `references/departments.csv` (300 valid codes) |
| From Date | `ZZ_SCPRS_SP_WRK_FROM_DATE` | `MM/DD/YYYY`; filters on each record's **Start Date** |
| To Date | `ZZ_SCPRS_SP_WRK_TO_DATE` | `MM/DD/YYYY` |
| Search | `ZZ_SCPRS_SP_WRK_BUTTON` | runs the query |

Other available filters: Supplier ID/Name, Purchase Document #, Description,
Acquisition Type/Method (useful for splitting oversized result sets — see below).

## Why a headless browser is required
Two behaviors defeat a plain `requests` scrape:
1. **Date fields reject programmatic input.** Setting the value via JS/`fill`
   is silently discarded by the field's edit mask; on search the date is blank
   and the filter is **ignored** (you get every record for the department,
   2000–present). The values must be **typed as real keystrokes** and committed
   with Tab. `src/scprs.py` does this and then *verifies* the values stuck.
2. **The download is a JS/modal flow.** Clicking a download button opens a
   PeopleSoft confirmation modal (`#ICOK`) that must be accepted before the
   file is generated; there is no direct file URL to fetch.

## The two downloads
- **Download Search Results** (`ZZ_SCPRS_SP_WRK_BUTTONS_GB`) → `Summary_Information_*.xls`
  — one row per purchase document, 18 columns (Grand Total, Supplier,
  Acquisition Type/Method, Buyer, Status, …).
- **Download Detail Information** (`ZZ_SCPRS_SP_WRK_BUTTON_BACKWARD`) → detail
  — one row per PO/line, 56 columns (line items + UNSPSC, unit price, supplier
  address, environmental/socioeconomic flags: EPP, SABRC, PCRC/TRC, SB/DVBE
  certification dates, Transaction Creation Date).

Both `.xls` files are actually **HTML tables**; parse with `pandas.read_html`
(handled by `load_extract`). Identifier columns carry a leading `'`; money is
`$1234.5`.

## The 65,000-row cap (the "more scraping?" answer)
A single download is capped at **65,000 rows**. If a Department + date range
exceeds that, a modal warns and only the first 65,000 rows are exported
(`Extract.truncated == True`). Large departments (e.g. 2660 Transportation)
blow past this even for a **single day**. To get complete data you must
**subdivide the date range** (or add an Acquisition Type/Method filter) until
each slice is under the cap, then concatenate. `download_extract` sets
`truncated` so callers can detect and re-slice automatically.
