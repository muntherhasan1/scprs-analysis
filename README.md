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
# Download + convert a Department + date-range extract to CSV (into data/):
python -m src.scprs 0250 06/01/2025 06/30/2025               # summary
python -m src.scprs 0250 06/01/2025 06/30/2025 --kind detail # line-item detail
```

- Business-unit codes: `references/departments.csv` (300 valid Departments).
- Dates are `MM/DD/YYYY` and filter on each record's **Start Date**.
- A single download is capped at **65,000 rows**. `src.model` (below) splits
  the date range automatically when it hits the cap; the low-level
  `download_extract` just flags it via `Extract.truncated`.

`src/scraper.py` remains as a generic CSV-link downloader for simpler sites
(timeouts, TLS on, path-traversal-safe filenames).

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

### Drill-down details (richer than the CSV exports)

The `Download Detail Information` CSV understates line-item dollars and omits
some fields. `src.model details` instead clicks each document's **PO Details**
page and loads three tables with the authoritative data:

```bash
python -m src.model details 8660 02/18/2021 02/18/2021
```

- `document_details` — one row per document, incl. **`bill_code`** (absent from
  both CSV exports) and clean totals.
- `document_lines` — line items whose `unit_price` sums to the grand total
  (item description, UNSPSC + description, quantity, line status).
- `document_pos` — associated POs with **per-PO** id, buyer, start date,
  PO total, and status.

Processes the documents in the results grid, so run it on narrow date ranges
(one document = one page load). Idempotent per document.

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
