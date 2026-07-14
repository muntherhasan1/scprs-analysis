<#
Batch PO-Details enrichment — drill line items for MANY business units,
newest-first.

Rotates over a list of business units, running for each
`python -m src.model enrich <BU> <From> <To> --limit <Days> --newest-first`.
Enrichment is resumable in model.py itself (each finished day is recorded in
details_progress and skipped next run), so re-running makes steady progress and a
day that errors retries later. The window defaults to the last 5 + next 2 fiscal
years (California FY = Jul 1 - Jun 30), processed newest-first — the recent data
most analysis cares about lands first.

Build the summary first (scripts\build_batch.ps1) so each BU has active days to
drill. Output appends to data/enrich_batch.log.

Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\enrich_batch.ps1 -Days 200
#>
param(
    [int]$Days = 200,
    [string]$From = "",   # blank -> start of the fiscal year five years ago
    [string[]]$BusinessUnits = @(
        "0820", "2660", "2720", "2740", "3540", "3600", "3790", "3860", "3960", "4260",
        "4265", "4300", "4440", "5180", "5225", "7100", "7730", "7760", "8570", "8950"
    )
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\enrich_batch.log"
Set-Location $root

# Rolling priority window: last 5 .. next 2 fiscal years from today.
$now = Get-Date
$fyStart = if ($now.Month -ge 7) { $now.Year } else { $now.Year - 1 }
if ([string]::IsNullOrEmpty($From)) { $From = "07/01/{0}" -f ($fyStart - 5) }
$to = "06/30/{0}" -f ($fyStart + 2)
function Log($m) { "$((Get-Date).ToString('o'))  $m" | Tee-Object -FilePath $log -Append }

Log "=== batch enrich start: $($BusinessUnits.Count) BUs, $From..$to --limit $Days --newest-first ==="
$i = 0
foreach ($bu in $BusinessUnits) {
    $i++
    Log "[$i/$($BusinessUnits.Count)] enrich $bu $From..$to --limit $Days --newest-first"
    try {
        & $py -m src.model enrich $bu $From $to --limit $Days --newest-first 2>&1 |
            ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -ne 0) { Log "  !! exit $LASTEXITCODE for $bu (will retry next run)" }
    }
    catch {
        Log "  !! error for $bu : $_ (will retry next run)"
    }
}
Log "=== batch enrich pass done ==="
