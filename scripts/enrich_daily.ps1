<#
Daily SCPRS enrichment runner (recent-data first).

Drills a few more active days of PO Details into data/scprs.db and appends to
data/enrich_daily.log. Resumable: each finished day is recorded, so every run
picks up where the last left off. Build the summary first
(`python -m src.model build <BU> ...`) so there are active days to enrich.

By default it prioritizes recent data: a rolling window of the last five fiscal
years plus the next two (California FY = Jul 1 - Jun 30), processed newest-first.
Older data still fills in once the recent window is exhausted (widen -From).

Run manually:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\enrich_daily.ps1 -Days 10
#>
param(
    [string]$BusinessUnit = "8660",
    [string]$From = "",       # blank -> start of the fiscal year five years ago
    [int]$Days = 10,
    [switch]$OldestFirst
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$log = Join-Path $root "data\enrich_daily.log"

# California fiscal year starts July 1. Build a rolling priority window:
# last 5 fiscal years .. next 2 fiscal years from today.
$now = Get-Date
$fyStart = if ($now.Month -ge 7) { $now.Year } else { $now.Year - 1 }
if ([string]::IsNullOrEmpty($From)) { $From = "07/01/{0}" -f ($fyStart - 5) }
$to = "06/30/{0}" -f ($fyStart + 2)
$newest = if ($OldestFirst) { @() } else { @("--newest-first") }

Set-Location $root
function Log($msg) { $msg | Out-File -FilePath $log -Append -Encoding utf8 }
Log "$(Get-Date -Format o)  enrich $BusinessUnit $From..$to --limit $Days $newest"
try {
    (& $py -m src.model enrich $BusinessUnit $From $to --limit $Days @newest 2>&1) |
        Out-File -FilePath $log -Append -Encoding utf8
    Log "$(Get-Date -Format o)  done (exit $LASTEXITCODE)"
}
catch {
    Log "$(Get-Date -Format o)  ERROR: $($_.Exception.Message)"
    throw
}
