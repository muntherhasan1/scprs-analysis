---
name: mart-builder
description: Create or change gold marts and expose them through the serving surface (MCP server, NL web app, reports). Use when adding a gold_* mart/view, improving how the NL/MCP front ends discover data, or deploying serving-layer changes to the Spaces.
---

# Mart building & serving: from gold table to answerable question

## Building a mart

- Marts reference the **`lv_*` logical views**, never physical `dim_*`/`fact_*`
  names (those are abbreviated post-build; `_to_logical_views` rewrites mart
  SQL at build time). Output column names are the public contract — keep them
  stable.
- **Vendor rollups use the canonical supplier marts** (`gold_supplier_master`,
  `gold_canonical_supplier_spend`) — per-`supplier_id` aggregation double-counts
  companies with multiple registrations.
- Contract change-over-time comes from `gold_contract_change_log` /
  `gold_contract_amendments` (built on append-only history), never from silver.
- Add a DQ check for the new mart in the same PR (severity `warn` unless a
  wrong answer would be silently plausible — then `error`).

## Making it discoverable (the NL/MCP half — don't skip this)

A mart nobody's model can find might as well not exist. Known failure: models
pick UNSPSC when the user means `acquisition_type` (category-shaped questions).
When adding a mart:
- Ensure `gold_data_dictionary` describes its columns meaningfully.
- Update the MCP tool surface hints (`list_marts` descriptions in
  `src/mcp_server.py`) with WHEN to use the mart and its key dimensions;
  surface distinct categorical values where ambiguity exists
  (`distinct_values` tool).
- Never duplicate the query guard into a front end — extend
  `src/warehouse_query.py` only (single source of truth: read-only connection,
  single-SELECT, object allowlist).

## Shipping to the Spaces

- The MCP Space runs **only** `deploy/hf-space/deploy.py`'s `COPIES` + lean
  `requirements-mcp.txt`. A new module-level import of an unshipped module or
  dep boot-crashes the Space (RUNTIME_ERROR — the 2026-07-21 config.py
  incident). Check the boot chain before touching shipped modules.
- Data refresh path: `warehouse build` → `serve-export` → publish (dedicated
  token) → **factory reboot** (a plain restart does NOT re-fetch) → `golive_check`
  verifies the live Space serves this build's markers.
- Charts/reports go through `src/charting.py` conventions; reports serve at
  capability URLs for the Copilot channel (`docs/COPILOT_STUDIO.md`).
