# Register a native Windows scheduled task for fleet usage refresh.
# Uses pwsh (PowerShell 7+), not Windows PowerShell 5.1.
# Interval defaults to 30 minutes (safely below the 2h capacity max_age).
#
# schtasks /TR is hard-capped at 261 characters on Windows. Task Scheduler's
# restricted PATH also cannot resolve bare "pwsh.exe" (0x80070002). We therefore:
#   - verify pwsh via Get-Command and embed the verified absolute .Source path
#     (quoted) into /TR so the task does not depend on PATH
#   - stage fleet_refresh_usage.ps1 to a short stable path under active
#     HermesHome (scripts\fleet_refresh_usage.ps1) so /TR never embeds an
#     ephemeral repo/worktree path
#   - keep flags minimal
#   - omit -HermesHome when it equals %LOCALAPPDATA%\hermes (refresher default)
#   - measure length and fail before schtasks /Create if still over the limit
param(
    [string]$TaskName = "HermesFleetUsageRefresh",
    [int]$IntervalMinutes = 30,
    [string]$HermesHome = $env:HERMES_HOME,
    [switch]$Remove,
    [switch]$DryRun,
    [switch]$StageOnly
)

$ErrorActionPreference = "Stop"

# Windows schtasks.exe rejects /TR values longer than this.
$SchtasksTrMax = 261

if ($Remove) {
    schtasks /Delete /TN $TaskName /F | Out-Null
    Write-Host "Removed scheduled task $TaskName"
    exit 0
}

if ($DryRun -and $StageOnly) {
    throw "Specify only one of -DryRun or -StageOnly"
}

if ($IntervalMinutes -lt 5 -or $IntervalMinutes -ge 120) {
    throw "IntervalMinutes must be in [5, 119] so refresh stays safely under 2h max_age"
}

$sourceScript = Join-Path $PSScriptRoot "fleet_refresh_usage.ps1"
if (-not (Test-Path -LiteralPath $sourceScript)) {
    throw "Missing refresher script: $sourceScript"
}
$sourceFull = (Resolve-Path -LiteralPath $sourceScript).Path

# Resolve PowerShell 7+ to an absolute path. Task Scheduler's restricted PATH
# cannot find bare "pwsh.exe" (Last Result -2147024894 / 0x80070002).
$pwshCmd = Get-Command pwsh -ErrorAction Stop
$pwshExe = $pwshCmd.Source
if ([string]::IsNullOrWhiteSpace($pwshExe)) {
    throw "Get-Command pwsh returned empty .Source"
}
if (-not (Test-Path -LiteralPath $pwshExe)) {
    throw "pwsh .Source does not exist: $pwshExe"
}
# Normalize to a rooted filesystem path (rejects relative / bare names).
$pwshFull = [System.IO.Path]::GetFullPath($pwshExe)
if (-not [System.IO.Path]::IsPathRooted($pwshFull)) {
    throw "pwsh path is not absolute: $pwshFull"
}
if (-not (Test-Path -LiteralPath $pwshFull)) {
    throw "pwsh absolute path does not exist: $pwshFull"
}

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

function Get-StagedRefresherPath {
    param([Parameter(Mandatory = $true)][string]$HomePath)
    # String-join (not Join-Path): custom -HermesHome may reference a drive that
    # does not exist yet; Join-Path fails closed on missing PSDrives.
    $homeNorm = ($HomePath -replace '/', '\').Trim().TrimEnd('\')
    $combined = "$homeNorm\scripts\fleet_refresh_usage.ps1"
    try {
        return [System.IO.Path]::GetFullPath($combined)
    } catch {
        return $combined
    }
}

function Stage-RefresherScript {
    <#
    .SYNOPSIS
      Atomically stage the refresher under HermesHome\scripts.
      Creates the directory, writes a unique temp sibling, then replaces the
      destination so the scheduled task never observes a partial script.
      Preserves exact source bytes (UTF-8 and any BOM intact).
    #>
    param(
        [Parameter(Mandatory = $true)][string]$SourcePath,
        [Parameter(Mandatory = $true)][string]$DestPath
    )

    if (-not (Test-Path -LiteralPath $SourcePath)) {
        throw "Staging source missing: $SourcePath"
    }

    $destDir = [System.IO.Path]::GetDirectoryName($DestPath)
    if ([string]::IsNullOrWhiteSpace($destDir)) {
        throw "Cannot determine staging directory for: $DestPath"
    }
    if (-not (Test-Path -LiteralPath $destDir)) {
        New-Item -ItemType Directory -Path $destDir -Force | Out-Null
    }
    if (-not (Test-Path -LiteralPath $destDir)) {
        throw "Failed to create staging directory: $destDir"
    }

    $tempName = ".fleet_refresh_usage." + [guid]::NewGuid().ToString("N") + ".tmp"
    $tempPath = Join-Path $destDir $tempName
    try {
        $bytes = [System.IO.File]::ReadAllBytes($SourcePath)
        if ($null -eq $bytes -or $bytes.Length -lt 1) {
            throw "Staging source is empty: $SourcePath"
        }
        # Write full payload to a same-directory temp file, then replace.
        [System.IO.File]::WriteAllBytes($tempPath, $bytes)
        if (-not (Test-Path -LiteralPath $tempPath)) {
            throw "Temp staged file missing after write: $tempPath"
        }
        $tempLen = (Get-Item -LiteralPath $tempPath).Length
        if ($tempLen -ne $bytes.Length) {
            throw "Temp staged size mismatch: wrote $($bytes.Length) got $tempLen"
        }
        # Same-volume replace: Move over destination. Avoid File.Replace(null backup)
        # which rejects empty backup path on some .NET hosts.
        if (Test-Path -LiteralPath $DestPath) {
            $backupName = ".fleet_refresh_usage.bak." + [guid]::NewGuid().ToString("N")
            $backupPath = Join-Path $destDir $backupName
            try {
                [System.IO.File]::Replace($tempPath, $DestPath, $backupPath)
            } finally {
                if (Test-Path -LiteralPath $backupPath) {
                    Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
                }
                if (Test-Path -LiteralPath $tempPath) {
                    Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
                }
            }
        } else {
            [System.IO.File]::Move($tempPath, $DestPath)
        }
    } catch {
        if (Test-Path -LiteralPath $tempPath) {
            Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        }
        throw
    }

    if (-not (Test-Path -LiteralPath $DestPath)) {
        throw "Staged refresher missing after replace: $DestPath"
    }
    $srcBytes = [System.IO.File]::ReadAllBytes($SourcePath)
    $dstBytes = [System.IO.File]::ReadAllBytes($DestPath)
    if ($srcBytes.Length -ne $dstBytes.Length) {
        throw "Staged refresher byte-length mismatch vs source"
    }
    for ($i = 0; $i -lt $srcBytes.Length; $i++) {
        if ($srcBytes[$i] -ne $dstBytes[$i]) {
            throw "Staged refresher content mismatch vs source at offset $i"
        }
    }
    return (Resolve-Path -LiteralPath $DestPath).Path
}

$resolvedHome = Normalize-FleetHomePath -PathValue $HermesHome
$resolvedDefault = Normalize-FleetHomePath -PathValue $defaultHome
$includeHermesHome = -not $resolvedHome.Equals(
    $resolvedDefault,
    [System.StringComparison]::OrdinalIgnoreCase
)

# Always schedule the short stable staged path under HermesHome\scripts — never
# an ephemeral repo/worktree path from $PSScriptRoot.
$stagedScript = Get-StagedRefresherPath -HomePath $resolvedHome

# /TR: quoted absolute pwsh + minimal flags + quoted -File path to staged script.
# -ExecutionPolicy Bypass keeps the task runnable under Restricted hosts.
# -WindowStyle Hidden avoids a console flash at each interval tick.
$trParts = [System.Collections.Generic.List[string]]::new()
$trParts.Add(('"{0}"' -f $pwshFull))
$trParts.Add('-NoProfile')
$trParts.Add('-WindowStyle')
$trParts.Add('Hidden')
$trParts.Add('-ExecutionPolicy')
$trParts.Add('Bypass')
$trParts.Add('-File')
$trParts.Add(('"{0}"' -f $stagedScript))
if ($includeHermesHome) {
    $trParts.Add('-HermesHome')
    $trParts.Add(('"{0}"' -f $resolvedHome))
}
$tr = [string]::Join(' ', $trParts)

if ($tr.Length -gt $SchtasksTrMax) {
    throw (
        "schtasks /TR length $($tr.Length) exceeds Windows limit $SchtasksTrMax. " +
        "Shorten -HermesHome or move HermesHome to a shorter path. TR=$tr"
    )
}

if ($DryRun) {
    # DryRun must not mutate or stage files; only calculate/render targets.
    Write-Host "STAGED_PATH=$stagedScript"
    Write-Host "SOURCE_PATH=$sourceFull"
    Write-Host "TR_LENGTH=$($tr.Length)"
    Write-Host "TR=$tr"
    Write-Host "INCLUDE_HERMES_HOME=$includeHermesHome"
    Write-Host "PWSH=$pwshFull"
    exit 0
}

# Stage before any task replacement so a failed stage never leaves a broken task.
try {
    $stagedResolved = Stage-RefresherScript -SourcePath $sourceFull -DestPath $stagedScript
} catch {
    throw "Refresher staging failed before task registration: $($_.Exception.Message)"
}

if ($StageOnly) {
    Write-Host "STAGED_PATH=$stagedResolved"
    Write-Host "SOURCE_PATH=$sourceFull"
    Write-Host "TR_LENGTH=$($tr.Length)"
    Write-Host "TR=$tr"
    Write-Host "INCLUDE_HERMES_HOME=$includeHermesHome"
    Write-Host "PWSH=$pwshFull"
    Write-Host "STAGE_ONLY=True"
    exit 0
}

$start = (Get-Date).AddMinutes(1).ToString("HH:mm")

schtasks /Create /TN $TaskName /TR $tr /SC MINUTE /MO $IntervalMinutes /ST $start /F | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "schtasks create failed with exit $LASTEXITCODE"
}

Write-Host "Registered $TaskName every $IntervalMinutes minutes via pwsh"
Write-Host "STAGED_PATH=$stagedResolved"
Write-Host "SOURCE_PATH=$sourceFull"
Write-Host "TR_LENGTH=$($tr.Length)"
Write-Host "TR=$tr"
Write-Host "PWSH=$pwshFull"
exit 0
