# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the maintainer rather than
opening a public issue. Include steps to reproduce and the potential impact.

## Secrets management

- **Never commit secrets.** Credentials live only in a git-ignored `.env`
  locally, or in the deployment platform's secret store in production.
- `.env.example` documents required variables with placeholder values.
- `gitleaks` runs as a pre-commit hook and in CI to block accidental leaks.
- All config is read through `src/config.py`, which fails loudly on missing
  values and never logs secret contents.

## CI/CD hardening

- Workflows run with `permissions: contents: read` (least privilege); widen
  only per-job when a step demonstrably needs it.
- Every push/PR runs: `ruff` (lint), `bandit` (SAST), `pip-audit` (dependency
  CVE scan), and `gitleaks` (secret scan).
- Dependabot keeps Python packages and GitHub Actions patched weekly.
- For stronger supply-chain guarantees, pin Actions to full commit SHAs
  (Dependabot will keep the SHAs updated).

## Access control (recommended repo settings)

Configure these in GitHub → Settings once a remote is added:

- **Branch protection on `main`:** require PR review, require status checks
  (the CI job) to pass, disallow force-pushes, and require signed commits.
- **Least-privilege collaborators:** grant the minimum role each person needs;
  prefer teams over individual grants.
- **Enable** secret scanning and push protection, Dependabot alerts, and 2FA
  enforcement for the org/repo.
- **Deployment credentials:** use short-lived, scoped tokens (e.g. OIDC to your
  cloud) instead of long-lived static keys wherever possible.

## Data handling

- SCPRS extracts are treated as sensitive: the `data/` directory and common
  data file types are git-ignored so datasets never land in version control.
