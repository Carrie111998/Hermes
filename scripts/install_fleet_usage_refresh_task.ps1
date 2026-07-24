# Register a native Windows scheduled task for fleet usage refresh.
# Uses pwsh (PowerShell 7+), not Windows PowerShell 5.1.
# Interval defaults to 30 minutes (safely below the 2h capacity max_age).
param(
    [string]$TaskName = "HermesFleetUsageRefresh",
    [int]$IntervalMinutes = 30,
    [string]$HermesHome = $env:HERMES_HOME,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    Write-Host "Removed scheduled task $TaskName"
    exit 0
}

if ($IntervalMinutes -lt 5 -or $IntervalMinutes -ge 120) {
    throw "IntervalMinutes must be in [5, 119] so refresh stays safely under 2h max_age"
}

$script = Join-Path $PSScriptRoot "fleet_refresh_usage.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    throw "Missing refresher script: $script"
}

$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
if (-not $HermesHome -or [string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
}

$tr = "`"$pwsh`" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -HermesHome `"$HermesHome`""
$start = (Get-Date).AddMinutes(1).ToString("HH:mm")

schtasks /Create /TN $TaskName /TR $tr /SC MINUTE /MO $IntervalMinutes /ST $start /F | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "schtasks create failed with exit $LASTEXITCODE"
}

Write-Host "Registered $TaskName every $IntervalMinutes minutes via pwsh"
Write-Host "TR=$tr"
exit 0
