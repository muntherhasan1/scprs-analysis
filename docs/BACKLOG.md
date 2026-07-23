# Backlog

Small, deferred enhancements. Each entry: what, why, and where to start.

## Open

### Pre-deploy import smoke-test for the Space image
The `deploy-mcp` workflow verifies the Space *after* it deploys (`deploy_check`),
so a packaging gap only surfaces once the live Space has already crashed to
`RUNTIME_ERROR` — exactly what happened when the first code-CD deploy shipped a
`data_sync.py` that imported an unshipped `config.py` (incident 2026-07-21,
#38→#39). A cheap pre-deploy gate would catch it *before* touching production.
- **Why:** turn a live-Space outage into a red check on the PR / a failed
  pre-deploy step; the safety net (`deploy_check`) stays, but the common failure
  (a new module-level import of a module not in `deploy.py`'s `COPIES`, or a dep
  not in `requirements-mcp.txt`) is caught pre-merge.
- **Start:** a `deploy/hf-space/smoke_import.py` (or a step) that assembles the
  exact shipped file set into a temp dir, `pip install -r requirements-mcp.txt`
  into a throwaway venv, and imports the boot chain
  (`import src.mcp_server`, then exercise `_require_db`'s `from . import data_sync`
  path) — failing on any `ModuleNotFoundError`. Run it as the first step of
  `deploy-mcp.yml` (before `deploy.py`) and/or as a `push`/PR check on the shipped
  paths. The manual repro that found the incident: import the boot chain with a
  `builtins.__import__` shim that raises `ModuleNotFoundError` for the non-lean
  deps (e.g. `dotenv`). See [[space-deploy-shipped-files]] in memory and
  `docs/REMOTE_MCP.md`.

### Capture per-section row counts in `generate_report` audit records
The `generate_report` audit record logs the report `title` and section SQLs but
`rows: None` (row counts are per-section, not report-level).
- **Why:** richer audit/eval detail — know how much each report section returned.
- **Start:** in `src/mcp_server.py` `generate_report`, collect each section's
  `res["row_count"]` (or `len(rows)`) alongside `section_sqls`, and pass a
  `sections_detail`/`row_counts` field to `query_log.record_tool`.
- **Caveat:** `generate_report` is part of the Copilot/charts/reports cluster the
  retrospective flagged for freeze/retire (zero audit-log usage). Don't invest here
  unless that cluster is being kept.

## Done

### Retire the web-app keep-warm workflow — DONE 2026-07-23 (#55)
`webapp-keepwarm.yml` pinged the chat Space every 10 min, but the Space is
private/frozen — its `hf.space` URL serves HF's sign-in interstitial with HTTP
**200**, so the check was *falsely succeeding* against a login page (verified
2026-07-23: root returns 200 while the unauthenticated Spaces API returns
"Invalid username or password"). Removed rather than left as a dead check.
**Revival:** when the chat Space is made public again, restore the workflow from
git history (`git log --diff-filter=D -- .github/workflows/webapp-keepwarm.yml`)
and re-verify the ping hits the app, not an interstitial.

### Auto-restart the Spaces after a data refresh — DONE 2026-07-20
`src/data_sync.py restart-spaces` (best-effort `HfApi().restart_space`, factory
reboot, warn-never-fail) wired into the Wave 2c workflow and `refresh_pipeline.ps1`.
`HF_DEPLOY_TOKEN` created and added to `.env` + Actions; verified live (the loop
now factory-reboots the MCP Space and serves the new build automatically). The
chat Space 404s on restart by design — it's private/frozen (see `docs/WEB_APP.md`
freeze status once the Copilot cluster is retired).
