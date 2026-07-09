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
- A single download is capped at **65,000 rows**; if exceeded, the tool prints a
  truncation warning (`Extract.truncated`) — narrow the date range for full
  coverage.

`src/scraper.py` remains as a generic CSV-link downloader for simpler sites
(timeouts, TLS on, path-traversal-safe filenames).

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
