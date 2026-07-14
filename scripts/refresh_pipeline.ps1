<#
End-to-end online-data refresh (Phase 3 of the data pipeline).

Rebuilds the warehouse from data/scprs.db, exports the slim serving DB, publishes
it to the private HF Dataset, and restarts the Spaces so they re-fetch it on boot.
Optionally drills a newest-first enrichment slice first (-Enrich).

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
    if ($Enrich) {
        Step "enrich (newest-first)" {
            & powershell -NoProfile -ExecutionPolicy Bypass `
                -File (Join-Path $root "scripts\enrich_batch.ps1") -Days $EnrichDays
        }
    }
    Step "warehouse build" { & $py -m src.warehouse build }
    Step "serve-export" { & $py -m src.warehouse serve-export }
    $env:WAREHOUSE_DATASET = $Dataset
    Step "publish to dataset" { & $py -m src.data_sync publish --dataset $Dataset }
    # Best-effort: the data is already published (the critical step). Restarting the
    # Spaces so they re-fetch needs a WRITE-scoped HF_TOKEN (repo-write / an OAuth
    # login is not enough for the Space management API). If it's missing, the refresh
    # still succeeds — the Spaces pick up the new DB on their next restart.
    $env:REFRESH_SPACES = ($Spaces -join ",")
    Log "START restart spaces (best-effort)"
    & $py -c @"
import os
from huggingface_hub import HfApi
api = HfApi()
tok = os.environ.get('HF_TOKEN')
for s in os.environ['REFRESH_SPACES'].split(','):
    api.restart_space(s.strip(), token=tok)
    print('restarted', s.strip())
"@
    if ($LASTEXITCODE -ne 0) {
        Log ("WARN restart spaces failed (exit $LASTEXITCODE) — data IS published; " +
            "the Spaces will serve it on their next restart. Set a write-scoped HF_TOKEN " +
            "so the pipeline can restart them automatically.")
    }
    else { Log "OK    restart spaces" }
    Log "=== refresh done: serve DB published; Spaces re-fetch on restart ==="
}
catch {
    Log "ABORT: $_"
    exit 1
}
