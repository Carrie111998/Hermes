# Registers AIUsageCollector: runs the collector every 5 minutes at user logon.
#
# Provenance: the live AIUsageCollector task on the primary host was registered
# by this script but the script itself was never committed, so the task's
# parameters existed only in the Task Scheduler database. Committing it makes
# the registration reproducible and reviewable.
#
# The runner path is derived from $env:USERPROFILE rather than hardcoded: no
# other tracked script in scripts/ embeds a developer's home directory, and on
# the host this was written for the derived path is byte-identical to the
# original literal. Pass -Runner to point it elsewhere.
[CmdletBinding()]
param(
    [string]$Runner = (Join-Path $env:USERPROFILE '.hermes\bin\ai_usage_collector_run.ps1'),
    [string]$TaskName = 'AIUsageCollector'
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $Runner)) {
    throw "Runner script not found at '$Runner'. Pass -Runner <path> to override."
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Runner`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
# ExecutionTimeLimit is 6 minutes -- deliberately just above the 5-minute
# repetition interval, with -MultipleInstances IgnoreNew, so a collector run
# that wedges is killed before the next one is due and runs never stack.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 6) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'AI token/quota collector -> ai-tokens.json' -Force
Write-Host "Registered $TaskName"
