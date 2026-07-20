<#
End-to-end online-data refresh (Phase 3 of the data pipeline).

Rebuilds the warehouse from data/scprs.db, exports the slim serving DB, and
publishes it to the private HF Dataset. The always-on Spaces fetch the serve DB only
at boot, so reboot them (Space UI -> Settings -> Factory reboot) afterward to serve
the new data. Optionally drills a newest-first enrichment slice first (-Enrich).

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
#>
param(
    [switch]$Enrich,
    [int]$EnrichDays = 200,
    [string]$Dataset = "munther-hasan/scprs-warehouse-data",
    [string]$OperationalDataset = "munther-hasan/scprs-operational-db",
    [string[]]$Spaces = @(
        "munther-hasan/scprs-warehouse-mcp",
        "munther-hasan/scprs-warehouse-chat"
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
        # (here or in CI) would discard it. The CI cron's concurrency group does
        # not cover this local publish — avoid running during a scheduled slot
        # (17 past 02/08/14/20 UTC).
        Step "publish operational store" {
            & $py -m src.data_sync publish-operational --dataset $OperationalDataset
        }
    }
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
