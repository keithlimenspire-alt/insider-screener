# Registers a Windows Task Scheduler job that runs the daily ingest + alerts
# every evening at 22:30 LOCAL time. EDGAR posts a trading day's index around
# 22:05 US-Eastern (~10:05 next morning in Singapore); the exact run time
# doesn't matter much because --daily catches up the last 7 days and keeps
# re-ingesting recent days until their index is final.
#
# Run this yourself from an elevated-or-not PowerShell — it modifies YOUR
# scheduled tasks, so it is not executed automatically by any tooling:
#   powershell -ExecutionPolicy Bypass -File scripts\schedule_daily.ps1
#
# Remove with:  Unregister-ScheduledTask -TaskName "InsiderScreenerDaily"

$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "$root\scripts\daily_ingest.cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At 22:30
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName "InsiderScreenerDaily" -Action $action `
    -Trigger $trigger -Settings $settings `
    -Description "SEC Form 4 daily ingest + insider-cluster alerts"

Write-Host "Registered task 'InsiderScreenerDaily' (daily 22:30)."
Write-Host "Logs: $root\data\daily_ingest.log · Alerts: $root\data\alerts.jsonl"
