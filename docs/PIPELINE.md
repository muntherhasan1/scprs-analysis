# Data-refresh pipeline (local → dataset → Spaces)

How fresh SCPRS data reaches the always-on front ends without baking it into their
images or depending on the (intermittent) collection machine.

## The three actors

```
 collection machine (intermittent)        HF Dataset (always on)      Spaces (always on)
 ─────────────────────────────────        ──────────────────────      ──────────────────
 scrape → enrich → warehouse build         warehouse-serve.db   ──▶   fetch at boot → serve
   → serve-export → PUBLISH  ────────▶      (private, versioned)       (MCP + web app)
```

- **The machine** runs the pipeline and **publishes** the slim serving DB. Only
  needs to be on when refreshing.
- **The private HF Dataset** (`munther-hasan/scprs-warehouse-data`) is the durable
  home of `warehouse-serve.db`. Persists 24/7 regardless of the machine.
- **The Spaces** download the serve DB **from the Dataset** at startup
  (`src/data_sync.ensure_local_db`) — never from the machine. If the machine is
  off they keep serving the last-published snapshot; only *new* data waits.

This decouples **data refreshes** (a dataset push + a cheap Space restart) from
**code deploys** (an image rebuild), and shrinks the shipped artifact from the full
419 MB warehouse to the ~55 MB gold-only serve DB (`warehouse serve-export`, which
drops bronze/silver/history and materializes the few view-marts that depend on them).

## Manual refresh

```powershell
python -m src.warehouse build            # scprs.db -> warehouse.db
python -m src.warehouse serve-export     # -> warehouse-serve.db (slim)
python -m src.data_sync publish --dataset munther-hasan/scprs-warehouse-data
# then factory-reboot the Spaces (Space UI -> Settings) so they re-fetch the new DB
```

Or the whole build → export → publish chain in one script:

```powershell
powershell -File scripts\refresh_pipeline.ps1            # build → export → publish
powershell -File scripts\refresh_pipeline.ps1 -Enrich    # also drill newest-first line items first
```

The script publishes, then logs a `MANUAL STEP` reminder — it does **not**
auto-restart the Spaces (that needs a Spaces-management token; intentionally out of
scope). Reboot each Space once via the UI (Settings → Factory reboot) to serve the
new data.

## Scheduling (intermittent machine)

```powershell
powershell -File scripts\register_refresh_task.ps1 -At 03:00
```

Registers a Windows Scheduled Task with **StartWhenAvailable**, so a run missed
while the machine was off fires as soon as it is next on. The Spaces serve the
last-published data in between.

## Config

| Env / var | Where | Purpose |
|---|---|---|
| `WAREHOUSE_DATASET` | Space **variable** + pipeline | The private dataset id to fetch/publish. |
| `HF_TOKEN` (Space) | Space **secret** | Fine-grained token with **Read** on the (private) `scprs-warehouse-data` dataset — the Space needs it to fetch the serve-DB at boot. Keep it **read-only**; a wrong scope here fails boot (`RUNTIME_ERROR`). |
| `QUERY_LOG_TOKEN` (Space) | Space **secret** (optional) | Fine-grained token with **Write** on `scprs-query-log`, used only by `query_log`. Split out so `HF_TOKEN` can stay read-only (least privilege). Falls back to `HF_TOKEN` if unset. |
| `HF_WAREHOUSE_TOKEN` (machine) | repo-root `.env` | Fine-grained token with **Write** on `scprs-warehouse-data`, used by `data_sync publish` to push the serve DB. Loaded from `.env` (via `src/config`); falls back to `HF_TOKEN` if unset. The Space-deploy token is **not** enough — publishing to the dataset needs dataset write. |

Put the machine's pipeline tokens (`HF_WAREHOUSE_TOKEN`, and any others) in the
**repo-root git-ignored `.env`** — the scheduled task runs with `-WorkingDirectory`
at the repo root, so `src/config` loads them; no machine-wide token env var or cached
`hf auth login` is needed. The **Space reboot stays manual**: a scheduled run
publishes fresh data, but the Spaces keep serving the previous snapshot until you
factory-reboot them (Space UI → Settings → Factory reboot).

Unset `WAREHOUSE_DATASET` locally → `ensure_local_db` is a no-op and the local
`data/warehouse.db` is used, so dev and tests are unaffected.
