# Backlog

Small, deferred enhancements. Each entry: what, why, and where to start.

## Open

### Feed CMAS data to the CI warehouse build
The warehouse integrates CMAS (`gold_cmas_agreement`, `gold_supplier_cmas`) as an
optional side input, but a CI build produces **empty** CMAS marts because
`data/cmas.db` isn't in the operational dataset — only local builds see real CMAS
data.
- **Why:** so the deployed Spaces serve real CMAS supplier integration, not empty
  tables, after a device-free refresh.
- **Start:** mirror the `supplier_enrichment.db` pattern — a `publish-cmas`
  subcommand in `src/data_sync.py` (push `cmas.db` to the operational dataset) and
  a best-effort `fetch-cmas` in the enrich workflow before `warehouse build`.
  Bigger question to decide first: should CMAS be *refreshed* device-free too
  (run `src.cmas extract` in CI on a cadence), or stay a manual local extract that
  is merely published? See `docs/CMAS.md`.

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

### Auto-restart the Spaces after a data refresh — DONE 2026-07-20
`src/data_sync.py restart-spaces` (best-effort `HfApi().restart_space`, factory
reboot, warn-never-fail) wired into the Wave 2c workflow and `refresh_pipeline.ps1`.
`HF_DEPLOY_TOKEN` created and added to `.env` + Actions; verified live (the loop
now factory-reboots the MCP Space and serves the new build automatically). The
chat Space 404s on restart by design — it's private/frozen (see `docs/WEB_APP.md`
freeze status once the Copilot cluster is retired).
