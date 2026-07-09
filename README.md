# scprs-analysis

Analysis of SCPRS procurement data, built on a security-first foundation.

## Setup

```bash
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements-dev.txt
pre-commit install          # enable local secret/lint/SAST hooks

cp .env.example .env        # then fill in real values (never commit .env)
```

Activating the venv puts `ruff`, `pytest`, `bandit`, and `pre-commit` on your
PATH, so you can call them directly (no `python -m` prefix needed).

## Layout

```
src/config.py   safe secrets loading (env-based, fails loudly)
src/scraper.py  starter web scraper: finds + downloads CSVs into data/
tests/          pytest suite
data/           local datasets — git-ignored, never committed
.github/        hardened CI + Dependabot (dormant until a GitHub remote exists)
```

## Scraping SCPRS

The California FI$Cal SCPRS search is a stateful PeopleSoft app; `src/scprs.py`
drives it with a headless browser. See [docs/SCPRS_NOTES.md](docs/SCPRS_NOTES.md)
for how it works and why a browser is required.

```bash
# Download + convert the summary extract to CSV (into data/):
python -m src.scprs 0250 06/01/2025 06/30/2025
```

> The site's "Download Detail Information" Excel export is **not used** — it
> silently drops line-item value on multi-line documents. Line-item and
> associated-PO data come from the PO Details drill-down instead (below).

- Business-unit codes: `references/departments.csv` (300 valid Departments).
- Dates are `MM/DD/YYYY` and filter on each record's **Start Date**.
- A single download is capped at **65,000 rows**. `src.model` (below) splits
  the date range automatically when it hits the cap; the low-level
  `download_extract` just flags it via `Extract.truncated`.

`src/scraper.py` remains as a generic CSV-link downloader for simpler sites
(timeouts, TLS on, path-traversal-safe filenames).

## Data warehouse (medallion: bronze → silver → gold)

`src/warehouse.py` builds a layered analytical warehouse (`data/warehouse.db`)
from the operational `scprs.db` — raw **bronze** snapshots with lineage,
cleaned/conformed **silver** (current-version grain, parsed acquisition,
Unknown members, DQ flags), and a **gold** Kimball star schema (surrogate-keyed
`dim_*` + `fact_document`/`fact_line`/`fact_associated_po` + mart views). Every
build is logged in `dw_batch` with severity-tiered `dw_dq_results`.

```bash
python -m src.warehouse build   # bronze -> silver -> gold + data quality
python -m src.warehouse dq       # re-run data-quality checks
python -m src.warehouse info     # layer row counts + last batch
```

See [docs/WAREHOUSE.md](docs/WAREHOUSE.md) for the full design.

## Deployment (Docker)

The pipeline is containerized (`Dockerfile`) so it can run off a personal machine
as a scheduled cloud job. The image bundles Chromium (via Playwright) and runs as
a non-root user with `--no-sandbox` (set by `PLAYWRIGHT_NO_SANDBOX`).

```bash
docker build -t scprs-analysis .

# SQLite files live under /app/data — mount a volume so they persist:
docker run --rm -v scprs-data:/app/data scprs-analysis src.warehouse info
docker run --rm -v scprs-data:/app/data scprs-analysis \
  src.model enrich 8660 07/01/2021 06/30/2028 --limit 200 --newest-first
docker run --rm -v scprs-data:/app/data scprs-analysis src.warehouse build
```

`docker run <image> <module> <args…>` maps to `python -m <module> <args…>`.

**Toward the cloud:** run the container as a scheduled job (Cloud Run Jobs / ECS
Fargate / Container Apps Jobs), point the warehouse at a managed Postgres
(bronze/silver/gold become schemas), keep raw extracts in object storage, and
wire build/deploy through the existing GitHub Actions CI. See the repo's issues
/ roadmap for the migration steps.

## Queryable data model (SQLite)

`src/model.py` pulls a business unit over a date range (auto-splitting past the
65k cap) into a local SQLite database (`data/scprs.db`) as a `purchases` table
— one row per purchase document — plus rollup views. Re-running a business unit
refreshes just that unit.

```bash
# Build / refresh the model for a business unit + date range:
python -m src.model build 8660 01/01/2016 07/08/2026

# Ad-hoc SQL:
python -m src.model query "SELECT supplier_name, document_count, total_value
                           FROM v_supplier_totals WHERE business_unit='8660'
                           ORDER BY total_value DESC LIMIT 10"

# Schema + loaded-data summary:
python -m src.model info
```

Table `purchases` columns: business_unit, department_name, purchase_document,
start_date, end_date, grand_total, supplier_id/name, acquisition_type_sub_type,
acquisition_method, buyer_name/email, status, version, … Views:
`v_supplier_totals`, `v_method_totals`, `v_monthly_totals`. Indexed on
business_unit, start_date, supplier, and acquisition_method.

### Drill-down details (authoritative — the Excel export is not)

The site's detail Excel understates line-item dollars (verified: it exported
$22,680 of a $482,500 contract). `src.model details` instead clicks each
document's **PO Details** page and loads three tables with the authoritative
data — its line items always reconcile to the merchandise amount:

```bash
python -m src.model details 8660 02/18/2021 02/18/2021

# Inspect one document exactly like the PO Details page (from the DB):
python -m src.model document 63626           # id or suffix
python -m src.model document 63626 --fetch   # drill it now if not yet enriched
```

- `document_details` — one row per document, incl. **`bill_code`** (absent from
  both CSV exports) and clean totals.
- `document_lines` — line items whose `unit_price` sums to the grand total
  (item description, UNSPSC + description, quantity, line status).
- `document_pos` — associated POs with **per-PO** id, buyer, start date,
  PO total, and status.

Processes the documents in the results grid, so run it on narrow date ranges
(one document = one page load). Idempotent per document.

To enrich a whole business unit, use the **day-by-day driver** — it only visits
days that actually have documents (distinct `start_date`s already in
`purchases`, so build the summary first) and records each finished day in
`details_progress`, so it **resumes** after an interruption:

```bash
python -m src.model build   8660 01/01/2016 07/08/2026   # summary first
python -m src.model enrich  8660 01/01/2016 07/08/2026   # then drill, resumable
python -m src.model enrich  8660 01/01/2016 07/08/2026 --limit 50   # a chunk at a time
```

A day that errors is left unrecorded and retried on the next run; `--force`
re-processes days already done. `--newest-first` drills the most recent days
first (recent-data priority).

To target a subset, `--acq-type` narrows the run to days that have at least one
document of a given acquisition type (a SQL `LIKE` pattern):

```bash
# Only drill days with an IT Services document (FY2021 onward, newest first):
python -m src.model enrich 8660 07/01/2021 06/30/2028 \
    --acq-type "IT Services%" --newest-first
```

The drill still loads **every** document on each selected day (the SCPRS search
grid can't filter by acquisition type) — the flag only chooses which days to
visit, cutting a targeted pass from every active day down to the days that
matter. Day completion is recorded per business unit, so filtered and unfiltered
runs share the same `details_progress` and never re-drill a finished day.

### Run it a little every day (Windows Task Scheduler)

`scripts/enrich_daily.ps1` drills a few more active days and appends to
`data/enrich_daily.log`. Register it to run daily:

```powershell
$script  = "C:\Users\munth\scprs-analysis\scripts\enrich_daily.ps1"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
             -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`" -Days 10"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM
Register-ScheduledTask -TaskName "SCPRS Enrich 8660" -Action $action -Trigger $trigger `
  -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable) -Force
```

`-Days N` sets how many active days to drill per run. Manage it with:

```powershell
Get-ScheduledTaskInfo "SCPRS Enrich 8660"   # last/next run + result
Start-ScheduledTask    "SCPRS Enrich 8660"  # run now
Unregister-ScheduledTask "SCPRS Enrich 8660" -Confirm:$false   # remove
```

The job only drills days already in `purchases`. To pick up **newly published**
documents, re-run `python -m src.model build 8660 …` periodically (SCPRS
refreshes every 24h) — that refreshes the summary and adds any new active days
the enricher will then drill.

## Security

Secrets management, CI/CD hardening, and access-control practices are documented
in [SECURITY.md](SECURITY.md). Key points:

- Secrets stay in a git-ignored `.env` (local) or the platform secret store
  (deployed) — loaded via `src/config.py`, never hard-coded.
- `pre-commit` and CI run `gitleaks`, `bandit`, `pip-audit`, and `ruff`.
- Enable branch protection + secret-scanning on the remote (see SECURITY.md).

## Checks

```bash
ruff check .
bandit -c pyproject.toml -r src
pip-audit -r requirements.txt
pytest
```
