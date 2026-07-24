# Register a native Windows scheduled task for fleet usage refresh.
# Uses pwsh (PowerShell 7+), not Windows PowerShell 5.1.
# Interval defaults to 30 minutes (safely below the 2h capacity max_age).
#
# schtasks /TR is hard-capped at 261 characters on Windows. We therefore:
#   - verify pwsh exists via Get-Command, but put only the short command name
#     "pwsh.exe" into /TR (PATH lookup at task runtime -- avoids long
#     WindowsApps paths blowing the budget)
#   - keep flags minimal
#   - omit -HermesHome when it equals %LOCALAPPDATA%\hermes (refresher default)
#   - measure length and fail before schtasks /Create if still over the limit
param(
    [string]$TaskName = "HermesFleetUsageRefresh",
    [int]$IntervalMinutes = 30,
    [string]$HermesHome = $env:HERMES_HOME,
    [switch]$Remove,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# Windows schtasks.exe rejects /TR values longer than this.
$SchtasksTrMax = 261

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
$scriptFull = (Resolve-Path -LiteralPath $script).Path

# Verify PowerShell 7+ is installed. Do NOT embed .Source into /TR -- store
# installs under WindowsApps yield paths that alone can exceed the 261 cap.
$null = Get-Command pwsh -ErrorAction Stop

$defaultHome = Join-Path $env:LOCALAPPDATA "hermes"
if (-not $HermesHome -or [string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHome = $defaultHome
}

function Normalize-FleetHomePath {
    param([Parameter(Mandatory = $true)][string]$PathValue)
    $raw = $PathValue.Trim()
    # Prefer a filesystem-resolved path when the directory exists; otherwise
    # normalize separators / relative segments without requiring existence
    # (custom -HermesHome may not exist yet at install time).
    try {
        if (Test-Path -LiteralPath $raw) {
            return (Resolve-Path -LiteralPath $raw).Path.TrimEnd('\')
        }
    } catch {
        # fall through
    }
    try {
        return [System.IO.Path]::GetFullPath($raw).TrimEnd('\')
    } catch {
        return ($raw -replace '/', '\').Trim().TrimEnd('\')
    }
}

$resolvedHome = Normalize-FleetHomePath -PathValue $HermesHome
$resolvedDefault = Normalize-FleetHomePath -PathValue $defaultHome
$includeHermesHome = -not $resolvedHome.Equals(
    $resolvedDefault,
    [System.StringComparison]::OrdinalIgnoreCase
)

# Short, stable /TR: command-name form + minimal flags + quoted -File path.
# -ExecutionPolicy Bypass keeps the task runnable under Restricted hosts.
# -WindowStyle Hidden avoids a console flash at each interval tick.
$trParts = [System.Collections.Generic.List[string]]::new()
$trParts.Add('pwsh.exe')
$trParts.Add('-NoProfile')
$trParts.Add('-WindowStyle')
$trParts.Add('Hidden')
$trParts.Add('-ExecutionPolicy')
$trParts.Add('Bypass')
$trParts.Add('-File')
$trParts.Add(('"{0}"' -f $scriptFull))
if ($includeHermesHome) {
    $trParts.Add('-HermesHome')
    $trParts.Add(('"{0}"' -f $resolvedHome))
}
$tr = [string]::Join(' ', $trParts)

if ($tr.Length -gt $SchtasksTrMax) {
    throw (
        "schtasks /TR length $($tr.Length) exceeds Windows limit $SchtasksTrMax. " +
        "Shorten -HermesHome or move the installer/refresher to a shorter path. TR=$tr"
    )
}

if ($DryRun) {
    Write-Host "TR_LENGTH=$($tr.Length)"
    Write-Host "TR=$tr"
    Write-Host "INCLUDE_HERMES_HOME=$includeHermesHome"
    exit 0
}

$start = (Get-Date).AddMinutes(1).ToString("HH:mm")

schtasks /Create /TN $TaskName /TR $tr /SC MINUTE /MO $IntervalMinutes /ST $start /F | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "schtasks create failed with exit $LASTEXITCODE"
}

Write-Host "Registered $TaskName every $IntervalMinutes minutes via pwsh"
Write-Host "TR_LENGTH=$($tr.Length)"
Write-Host "TR=$tr"
exit 0
