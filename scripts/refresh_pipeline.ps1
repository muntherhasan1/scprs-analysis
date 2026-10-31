<#
End-to-end online-data refresh (Phase 3 of the data pipeline).

Optionally drills a newest-first enrichment slice (-Enrich) and publishes it to
the operational dataset, then asks CI to rebuild and publish the serve DB by
dispatching "Warehouse refresh (Wave 2c)" (needs the gh CLI, logged in).

CI is the single serve publisher: warehouse-refresh.yml runs in the
warehouse-serve-publisher concurrency group and verifies go-live with an
evidence-graded rollback. A local serve publish has neither — it can overwrite a
NEWER CI publish with this machine's older build, and nothing checks the Spaces
afterwards. -LocalServePublish keeps the old local build -> publish -> restart
path for when CI is unavailable; check no warehouse refresh is running first.

Built for Windows Task Scheduler on the (intermittent) collection machine: the
always-on Spaces serve whatever was last published, so a missed/late run only
delays the next refresh — nothing goes down. Register it with
scripts\register_refresh_task.ps1 (uses "start when available" so a missed run
fires as soon as the machine is next on). Logs to data\refresh_pipeline.log.

Any step that fails aborts the run BEFORE publishing, so a broken build never
reaches the Spaces.

Run manually:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refresh_pipeline.ps1
    ... -Enrich -EnrichDays 200      # also deepen line-item detail first
    ... -LocalServePublish           # build + publish the serve DB from this machine
#>
param(
    [switch]$Enrich,
    [switch]$LocalServePublish,
    [int]$EnrichDays = 200,
    [string]$Dataset = "munther-hasan/scprs-warehouse-data",
    [string]$OperationalDataset = "munther-hasan/scprs-operational-db",
    # The NL chat Space was retired 2026-10-31 (paused); see docs/WEB_APP.md.
    [string[]]$Spaces = @(
        "munther-hasan/scprs-warehouse-mcp"
    )
)
# Continue, not Stop: these steps are native commands (python/powershell) and some
# write normal progress to stderr (e.g. the HF upload bar). Under "Stop" PS 5.1
# turns that stderr into a fatal error. We gate success on $LASTEXITCODE per step
# instead, which is the correct signal for a native command.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\refresh_pipeline.log"
Set-Location $root
function Log($m) { "$((Get-Date).ToString('o'))  $m" | Tee-Object -FilePath $log -Append }

function Step($label, [scriptblock]$body) {
    Log "START $label"
    & $body
    if ($LASTEXITCODE -ne 0) { Log "ABORT: $label failed (exit $LASTEXITCODE)"; exit 1 }
    Log "OK    $label"
}

Log "=== refresh start (enrich=$Enrich) ==="
try {
    # Wave 2c single-writer model: CI owns scprs.db (the HF dataset copy is the
    # source of truth). Always refresh the local copy first so the warehouse
    # builds on CI's latest enrichment, not a stale local file.
    Step "fetch operational store" {
        & $py -m src.data_sync fetch-operational --dataset $OperationalDataset
    }
    if ($Enrich) {
        Step "enrich (newest-first)" {
            & powershell -NoProfile -ExecutionPolicy Bypass `
                -File (Join-Path $root "scripts\enrich_batch.ps1") -Days $EnrichDays
        }
        # Local enrichment must flow back to the dataset, or the next fetch
        # (here or in CI) would discard it. This publish is compare-and-swap
        # against the revision fetched above: if a CI writer committed in the
        # meantime it is REFUSED (the local drill is lost, CI's data is kept).
        # CI writers fire at unpredictable times (crons run hours late), so
        # check `gh run list --workflow "Enrich (Wave 2)"` before a long -Enrich.
        Step "publish operational store" {
            & $py -m src.data_sync publish-operational --dataset $OperationalDataset
        }
    }
    if (-not $LocalServePublish) {
        Step "dispatch CI warehouse refresh" {
            & gh workflow run "Warehouse refresh (Wave 2c)"
        }
        Log ("=== refresh done: CI warehouse refresh dispatched (it rebuilds, publishes, " +
            "restarts the Spaces and verifies go-live) ===")
        exit 0
    }
    Log "WARNING -LocalServePublish: no CAS, outside the CI publisher lock, no go-live check"
    Step "warehouse build" { & $py -m src.warehouse build }
    Step "serve-export" { & $py -m src.warehouse serve-export }
    # publish reads the write-scoped HF_WAREHOUSE_TOKEN from the repo-root .env (via
    # src/config); no HF token is set in this process's environment.
    Step "publish to dataset" { & $py -m src.data_sync publish --dataset $Dataset }
    # The serve DB is published (the critical step). The always-on Spaces only fetch
    # it at boot; try a best-effort restart so the new data goes live (needs
    # HF_DEPLOY_TOKEN in .env — without it each Space prints FAILED and the exit
    # code stays 0). Never gates the run: the publish already succeeded.
    (& $py -m src.data_sync restart-spaces 2>&1) | Tee-Object -FilePath $log -Append
    Log ("If a restart FAILED above, reboot manually (Space UI -> Settings -> " +
        "Factory reboot): " + ($Spaces -join ', '))
    Log "=== refresh done: serve DB published ==="
}
catch {
    Log "ABORT: $_"
    exit 1
}
