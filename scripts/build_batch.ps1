<#
Batch SCPRS summary builder — first-load many NEW business units.

Each unit gets ONE `python -m src.model build <BU> 07/01/<FromFY-1> 06/30/<ToFY>`
call over its whole window (California FY = Jul 1 - Jun 30, labelled by the year
it ends); the scraper bisects internally to stay under the 65k-row export cap.

Why one call per unit: `model build` REPLACES a unit's whole summary with the
window it is given. This script used to loop fiscal year by fiscal year, so each
call deleted the year before it and every unit ended up holding only its oldest
fiscal year (2026-10 review). Never call `model build` per sub-window.

Units that already have summary rows are SKIPPED: keep those current with
`python -m src.model refresh` (the daily summary-refresh.yml cron), which derives
each unit's full window itself and gates the scrape before writing. Re-building a
loaded unit by hand risks exactly the narrower-window data loss above.

Resumable: every finished unit is recorded in data/build_batch_progress.txt and
skipped on re-run; a call that errors is logged and left unrecorded so it retries
next run. Progress + output append to data/build_batch.log.

Enrichment (PO Details drill-down) is a SEPARATE, slower stage — run it
newest-first via enrich_batch.ps1 after this.

Run:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_batch.ps1 -BusinessUnits 1234,5678
#>
param(
    [int]$FromFY = 2022,   # oldest fiscal year to load (FY ending 06/30/2022)
    [int]$ToFY   = 2028,   # newest fiscal year to load (FY ending 06/30/2028)
    [Parameter(Mandatory = $true)]
    [string[]]$BusinessUnits
)
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\build_batch.log"
$progress = Join-Path $root "data\build_batch_progress.txt"
Set-Location $root
if (-not (Test-Path $progress)) { New-Item -ItemType File -Path $progress | Out-Null }
function Log($m) { "$((Get-Date).ToString('o'))  $m" | Tee-Object -FilePath $log -Append }

# Rows already stored for a unit (0 when the store or table does not exist yet).
# Single quotes only: PowerShell 5.1 strips embedded double quotes from native args.
$countRows = @'
import sqlite3, sys
try:
    con = sqlite3.connect('file:data/scprs.db?mode=ro', uri=True)
    q = 'SELECT COUNT(*) FROM purchases WHERE business_unit = ?'
    print(con.execute(q, (sys.argv[1],)).fetchone()[0])
except sqlite3.Error:
    print(0)
'@

$done = @{}
Get-Content $progress | ForEach-Object { if ($_.Trim()) { $done[$_.Trim()] = $true } }

$from = "07/01/{0}" -f ($FromFY - 1)
$to = "06/30/{0}" -f $ToFY
Log "=== batch build start: $($BusinessUnits.Count) BUs, one $from..$to load each ==="
$n = 0
foreach ($bu in $BusinessUnits) {
    $n++
    if ($done[$bu]) { Log "[$n] skip $bu (done)"; continue }
    $existing = & $py -c $countRows $bu
    if ($LASTEXITCODE -ne 0 -or -not ($existing -match '^\d+$')) {
        Log "  !! could not read the store for $bu — not loading (will retry next run)"
        continue
    }
    if ([int]$existing -gt 0) {
        Log "[$n] skip $bu ($existing rows already loaded — use 'python -m src.model refresh', not build)"
        continue
    }
    Log "[$n] build $bu $from..$to"
    try {
        & $py -m src.model build $bu $from $to 2>&1 | ForEach-Object { Log "  $_" }
        if ($LASTEXITCODE -eq 0) { $bu | Out-File -FilePath $progress -Append -Encoding utf8 }
        else { Log "  !! exit $LASTEXITCODE for $bu (will retry next run)" }
    }
    catch {
        Log "  !! error for $bu : $_ (will retry next run)"
    }
}
Log "=== batch build done ==="
