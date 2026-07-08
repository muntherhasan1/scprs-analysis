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

## Layout

```
src/            application + analysis code (src/config.py loads secrets safely)
tests/          pytest suite
data/           local datasets — git-ignored, never committed
.github/        hardened CI + Dependabot
```

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
