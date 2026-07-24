---
name: data-extraction
description: Add or fix a data source (scraper, API, Power BI embed) for this pipeline — recon-first protocol, extractor contract, resumability, publish gates, and the CI cron template. Use when ingesting a NEW external source (eProcure, budgets, FRED, news) or changing how an existing one (SCPRS, CMAS) is extracted.
---

# Data extraction: how sources get ingested here

## Recon FIRST, code second

Never write an extractor before proving the access mechanism with throwaway
scratchpad probes:

1. **Identify the mechanism.** REST/JSON API (best) → plain httpx. Power BI
   embed → query the model's DSR protocol directly like `src/cmas.py`
   (token → modelsAndExploration → QES query; see `docs/CMAS.md`). PeopleSoft
   component → Playwright like `src/scprs.py` (last resort).
2. **Verify formats against LIVE text, never assumptions.** Incident: the grid
   row-count banner is `"1 to 200 of 206"`; an assumed `"1-200 of"` regex never
   matched and silently hid multi-page truncation for weeks (#49).
3. **Probe pagination/limits explicitly**: page size, next controls (custom
   buttons vs standard chunking), whether ordering is stable across sessions
   (SCPRS's is NOT — track entities by id, never position), row caps (SCPRS
   exports cap at 65,000 → bisection), and server round-trip latency behind
   glass panes (poll for content change, don't sleep a fixed time).
4. **Distrust convenient exports.** SCPRS's "Download Detail Information"
   silently drops line-item dollars on multi-line documents — verified by
   reconciling one document ($22,680 exported of a $482,500 contract). Always
   reconcile a sample against the site's own detail pages before trusting a
   bulk export.

## The extractor contract (what CMAS proved, every source follows)

- Standalone module `src/<source>.py`, own `data/<source>.db`, `python -m
  src.<source>` CLI. Never entangle with other sources' stores.
- **Idempotent per entity**: reloading an entity replaces its rows (scoped
  delete + insert), so re-extraction is always safe.
- **Resumable + budget-aware** if the extraction is long-running: skip sets of
  already-fetched entities, a `--budget-minutes` wall-clock stop that exits 0,
  and a `complete` flag so callers never record partial coverage as done (see
  `collect_po_details` / `enrich_details` — this pattern broke a 3-day
  livelock, PR #42).
- Money/date cleaning at load time (`$1,234` → float, MM/DD/YYYY → ISO).

## Publishing and gates

- Side-input DBs publish to the private operational HF dataset,
  **upload-on-success only** — a failed/partial run must never overwrite good
  data.
- **Shrink gate**: new extract must hold ≥90% of previously published rows
  (first publish passes on >0) — `cmas-refresh.yml` is the template. ">0
  rows" alone lets a truncated query destroy the store.
- Concurrent-writer safety: publishes to a fetched-then-mutated store must
  compare-and-swap (`parent_commit`, see `data_sync.publish_operational_db`).
- Dedicated least-privilege HF token per dataset/direction (see
  hf-token-scoping in memory).

## The CI cron template

Copy the shape of `.github/workflows/cmas-refresh.yml` (simple) or
`enrich-canary.yml` (long-running): offset cron; `scprs-operational-writer`
concurrency group if it writes the operational dataset; `PYTHONUNBUFFERED: "1"`;
`timeout-minutes` as a backstop ABOVE the self-budget; inputs via `env:` not
inline `${{ }}`; result + duration to `GITHUB_STEP_SUMMARY`. Then wire
observability: a `_HINTS` entry in `src/triage.py`, and confirm the pipeline
monitor / dead-man's switch cover the new workflow (see the pipeline-ops skill).

## Before merging

Live-test the full contract locally: one budget-capped run proving partial
banking, a second proving resume + completion, then verify row counts in the
DB against the source's own totals.
