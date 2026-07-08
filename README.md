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

## Scraping CSVs

```bash
# Download every .csv linked from a page into data/ (sanitized filenames):
python -m src.scraper https://example.gov/scprs/reports
```

`src/scraper.py` is a starting point — adjust the link-finding to match your
target page. It enforces request timeouts, keeps TLS verification on, and
sanitizes output filenames to prevent path traversal. Check a site's
robots.txt / terms before scraping it.

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
