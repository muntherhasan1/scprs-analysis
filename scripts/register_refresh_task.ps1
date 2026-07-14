<#
Register the SCPRS data refresh as a Windows Scheduled Task.

Runs scripts\refresh_pipeline.ps1 daily with **StartWhenAvailable**, so if the
machine was off at the scheduled time the run fires as soon as it is next on —
the right model for an intermittent collection box. The always-on Spaces keep
serving the last-published data in the meantime. Re-run to update settings.

Remove with:
    Unregister-ScheduledTask -TaskName "SCPRS data refresh" -Confirm:$false

Run once:
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_refresh_task.ps1
    ... -At 03:00 -Enrich      # also deepen line-item detail each run
#>
param(
    [string]$At = "03:00",
    [string]$TaskName = "SCPRS data refresh",
    [switch]$Enrich
)
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\refresh_pipeline.ps1"
$argline = "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
if ($Enrich) { $argline += " -Enrich" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argline -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# StartWhenAvailable: catch up a missed run when the machine returns. Cap runtime
# so a stuck run can't linger; the refresh itself is short unless -Enrich is set.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Force `
    -Description "Rebuild + publish the SCPRS serve DB, then restart the HF Spaces." | Out-Null
Write-Output "Registered '$TaskName': daily at $At, start-when-available (enrich=$Enrich)."
