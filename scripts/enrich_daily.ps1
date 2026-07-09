<#
Daily SCPRS enrichment runner.

Drills a few more active days of PO Details into data/scprs.db and appends to
data/enrich_daily.log. Resumable: each finished day is recorded, so every run
picks up where the last left off. Build the summary first
(`python -m src.model build <BU> ...`) so there are active days to enrich.

Run manually:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\enrich_daily.ps1 -Days 10
#>
param(
    [string]$BusinessUnit = "8660",
    [string]$From = "01/01/2016",
    [int]$Days = 10
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\enrich_daily.log"
$to = (Get-Date).ToString('MM/dd/yyyy')

Set-Location $root
function Log($msg) { $msg | Out-File -FilePath $log -Append -Encoding utf8 }
Log "$(Get-Date -Format o)  enrich $BusinessUnit $From..$to --limit $Days"
try {
    (& $py -m src.model enrich $BusinessUnit $From $to --limit $Days 2>&1) |
        Out-File -FilePath $log -Append -Encoding utf8
    Log "$(Get-Date -Format o)  done (exit $LASTEXITCODE)"
}
catch {
    Log "$(Get-Date -Format o)  ERROR: $($_.Exception.Message)"
    throw
}
