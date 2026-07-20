# Backlog

Small, deferred enhancements. Each entry: what, why, and where to start.

## Open

### Auto-restart the Spaces after a data refresh — SHIPPED 2026-07-20 (token pending)
`src/data_sync.py restart-spaces` implemented (best-effort `HfApi().restart_space`
per Space — warn, never fail) and wired into both the Wave 2c workflow and
`refresh_pipeline.ps1`. **Remaining one-time step:** create `HF_DEPLOY_TOKEN`
(fine-grained, write on both `scprs-warehouse-mcp` and `scprs-warehouse-chat`
Space repos) and add it to `.env` + as a GitHub Actions secret; until then each
restart prints FAILED and the Spaces need a manual reboot to serve new data.
The `add_space_secret` marker-secret fallback from the original design was
dropped: it needs the same write scope, so it cannot succeed where
`restart_space` fails.

### Capture per-section row counts in `generate_report` audit records
The `generate_report` audit record logs the report `title` and section SQLs but
`rows: None` (row counts are per-section, not report-level).
- **Why:** richer audit/eval detail — know how much each report section returned.
- **Start:** in `src/mcp_server.py` `generate_report`, collect each section's
  `res["row_count"]` (or `len(rows)`) alongside `section_sqls`, and pass a
  `sections_detail`/`row_counts` field to `query_log.record_tool`.
