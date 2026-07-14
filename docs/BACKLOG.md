# Backlog

Small, deferred enhancements. Each entry: what, why, and where to start.

## Open

### Web app (Model B) keep-warm + failure alert
Give the public NL web app Space the same watchdog the MCP Space already has, so
its Copilot/browser sessions don't hit a slept Space and its downtime is visible.
- **Why:** the MCP Space is covered by `.github/workflows/mcp-keepwarm.yml` (pings
  `/healthz` every 10 min, opens/auto-closes a `mcp-keepwarm-alert` GitHub issue on
  failure). The web app Space has no equivalent.
- **Start:** copy `mcp-keepwarm.yml` to a `webapp-keepwarm.yml`, point `URL` at the
  web app Space's health endpoint (confirm its path — Gradio may not expose
  `/healthz`; may need `/` or a lightweight route), and use a distinct alert label.

### Capture per-section row counts in `generate_report` audit records
The `generate_report` audit record logs the report `title` and section SQLs but
`rows: None` (row counts are per-section, not report-level).
- **Why:** richer audit/eval detail — know how much each report section returned.
- **Start:** in `src/mcp_server.py` `generate_report`, collect each section's
  `res["row_count"]` (or `len(rows)`) alongside `section_sqls`, and pass a
  `sections_detail`/`row_counts` field to `query_log.record_tool`.
