<#
Batch SCPRS summary builder — load many business units, most-recent fiscal year
first.

Outer loop is fiscal year DESCENDING, inner loop is business unit, so the newest
fiscal year is populated across every department before older years fill in
(California FY = Jul 1 - Jun 30, labelled by the year it ends). Each (BU, FY) is a
single `python -m src.model build <BU> 07/01/<fy-1> 06/30/<fy>` call; the scraper
bisects internally to stay under the 65k-row export cap.

Resumable + idempotent: every finished (BU, FY) is recorded in
data/build_batch_progress.txt and skipped on re-run; a call that errors is logged
and left unrecorded so it retries next run. Progress + output append to
data/build_batch.log.

Enrichment (PO Details drill-down) is a SEPARATE, slower stage — run it
newest-first via enrich_daily.ps1 after (or alongside) this.

Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_batch.ps1
#>
param(
    [int]$FromFY = 2022,   # oldest fiscal year to load (FY ending 06/30/2022)
    [int]$ToFY   = 2028,   # newest fiscal year to load (FY ending 06/30/2028)
    [string[]]$BusinessUnits = @(
        "0820", "2660", "2720", "2740", "3540", "3600", "3790", "3860", "3960", "4260",
        "4265", "4300", "4440", "5180", "5225", "7100", "7730", "7760", "8570", "8950"
    )
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\build_batch.log"
$progress = Join-Path $root "data\build_batch_progress.txt"
Set-Location $root
if (-not (Test-Path $progress)) { New-Item -ItemType File -Path $progress | Out-Null }
function Log($m) { "$((Get-Date).ToString('o'))  $m" | Tee-Object -FilePath $log -Append }

$done = @{}
Get-Content $progress | ForEach-Object { if ($_.Trim()) { $done[$_.Trim()] = $true } }

$fys = $ToFY..$FromFY   # descending: newest fiscal year first
$total = $fys.Count * $BusinessUnits.Count
Log "=== batch build start: $($BusinessUnits.Count) BUs x FY$FromFY..FY$ToFY (newest first), $total cells ==="
$n = 0
foreach ($fy in $fys) {
    $from = "07/01/{0}" -f ($fy - 1)
    $to = "06/30/{0}" -f $fy
    foreach ($bu in $BusinessUnits) {
        $n++
        $key = "$bu FY$fy"
        if ($done[$key]) { Log "[$n/$total] skip $key (done)"; continue }
        Log "[$n/$total] build $bu $from..$to"
        try {
            & $py -m src.model build $bu $from $to 2>&1 | ForEach-Object { Log "  $_" }
            if ($LASTEXITCODE -eq 0) { $key | Out-File -FilePath $progress -Append -Encoding utf8 }
            else { Log "  !! exit $LASTEXITCODE for $key (will retry next run)" }
        }
        catch {
            Log "  !! error for $key : $_ (will retry next run)"
        }
    }
}
Log "=== batch build done ==="
