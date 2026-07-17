# Backlog

Small, deferred enhancements. Each entry: what, why, and where to start.

## Open

### Auto-restart the Spaces after a data refresh
`scripts/refresh_pipeline.ps1` publishes the serve DB unattended, but the Spaces
only re-fetch it at boot — so a **manual** Factory reboot is currently required to
go live (deferred by choice when the publish automation shipped).
- **Why:** removes the one manual step in an otherwise hands-off refresh; lets the
  scheduled task make new data live without a human.
- **Start:** add a `restart-spaces` subcommand to `src/data_sync.py` — try
  `HfApi().restart_space(repo, token)`, and on failure fall back to writing a
  rotating value to a marker secret via `add_space_secret` (a changed secret
  triggers a Space restart — see `deploy/hf-space/set_tokens.py:129`). Read a
  settings-write token from `.env` (`HF_DEPLOY_TOKEN`) that covers **both**
  `scprs-warehouse-mcp` and `scprs-warehouse-chat`; call it from
  `refresh_pipeline.ps1` (best-effort — warn, never fail). A full design was
  scoped 2026-07-17 (see the plan agent output that session).

### Capture per-section row counts in `generate_report` audit records
The `generate_report` audit record logs the report `title` and section SQLs but
`rows: None` (row counts are per-section, not report-level).
- **Why:** richer audit/eval detail — know how much each report section returned.
- **Start:** in `src/mcp_server.py` `generate_report`, collect each section's
  `res["row_count"]` (or `len(rows)`) alongside `section_sqls`, and pass a
  `sections_detail`/`row_counts` field to `query_log.record_tool`.
