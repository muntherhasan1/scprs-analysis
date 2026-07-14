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
# then restart the Spaces so they re-fetch (or let the scheduled task do it)
```

Or all of it, with the Spaces restarted for you:

```powershell
powershell -File scripts\refresh_pipeline.ps1            # build → export → publish → restart
powershell -File scripts\refresh_pipeline.ps1 -Enrich    # also drill newest-first line items first
```

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
| `HF_TOKEN` | Space **secret** | HF token with **read** access to that dataset (the Space needs it to fetch). The pipeline publishes with the machine's cached HF login. |

Unset `WAREHOUSE_DATASET` locally → `ensure_local_db` is a no-op and the local
`data/warehouse.db` is used, so dev and tests are unaffected.
