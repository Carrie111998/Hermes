#requires -Version 5.1
<#
.SYNOPSIS
    Wraps the Hermes staged updater to break the Tauri-shell race that
    produces the "Another Hermes update is already running" loop on Windows.

.DESCRIPTION
    The desktop's applyUpdates() flow on Windows:
      1. spawnUpdaterProcess('hermes-setup.exe', ['--update','--branch','main'], ...)
      2. writeUpdateMarker with child.pid
      3. setTimeout(app.quit, UPDATE_HANDOFF_DWELL_MS)

    The spawned hermes-setup.exe is a Tauri app. Tauri re-execs the actual
    updater logic into a different OS process; the lock's PID does not match
    that inner process's std::process::id(), so the self-PID adoption check
    in update.rs:163 fails and the wrapper aborts.

    Closing Hermes manually first makes the loop go away (empirically verified).
    This wrapper inserts that "wait for Hermes to die" step programmatically.
    When the desktop spawns us, we wait for the desktop to fully exit
    (and the venv Python backend to release the venv shim), then exec the
    REAL installer (hermes-setup-real.exe) with the original args.

.NOTES
    - Idempotent: if the real installer is missing, exit 2 with a clear log
    - Bounded: 60s timeout on the wait, with periodic progress logging
    - Non-destructive: the real installer is never moved or modified
    - Reversible: deleting the wrapper files and renaming hermes-setup-real.exe
      back to hermes-setup.exe restores the stock state
#>

# NOTE: deliberately NOT using [CmdletBinding()] here — the wrapper receives
# arbitrary forward-passed args (--update, --branch <name>, etc.) from the
# desktop, and strict-mode parameter binding would reject them as "unknown
# parameter names". Plain script + $args is the correct shape.

$ErrorActionPreference = 'Stop'

$HERMES_HOME      = Join-Path $env:LOCALAPPDATA 'hermes'
$REAL_INSTALLER  = Join-Path $HERMES_HOME 'hermes-setup-real.exe'
$LOG_DIR         = Join-Path $HERMES_HOME 'logs'
$LOG_FILE        = Join-Path $LOG_DIR   'update-wrapper.log'
$TIMEOUT_SECONDS = 60
$POLL_SECONDS    = 1
$GRACE_SECONDS   = 1   # extra wait after Hermes exits, lets the venv shim unlock

if (-not (Test-Path -LiteralPath $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $ts   = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffzzz')
    $line = "$ts  $Message"
    Add-Content -LiteralPath $LOG_FILE -Value $line
    Write-Host $line
}

function Get-HermesProcesses {
    $hermes = @(Get-Process -Name 'Hermes' -ErrorAction SilentlyContinue)
    $node   = @(Get-Process -Name 'node' -ErrorAction SilentlyContinue | Where-Object {
                    $_.Path -and (
                        $_.Path -like '*\hermes-agent\*' -or
                        $_.Path -like '*\hermes\venv\*' -or
                        $_.Path -like '*\Local\hermes\*'
                    )
                })
    [pscustomobject]@{ Hermes = $hermes; NodeBackend = $node }
}

# ---- main ----

$argSummary = if ($args.Count -gt 0) { ($args -join ' ') } else { '(no args)' }
Write-Log ("wrapper invoked; args: " + $argSummary + "  pid: " + $PID + "  user: " + $env:USERNAME)

# 1) sanity: real installer must exist
if (-not (Test-Path -LiteralPath $REAL_INSTALLER)) {
    Write-Log ("FATAL: real installer not found at " + $REAL_INSTALLER)
    Write-Log "  (the wrapper is in place but hermes-setup-real.exe is missing --"
    Write-Log "   re-apply the wrapper, or restore hermes-setup-real.exe from backup)"
    exit 2
}

# 2) wait for the desktop to fully exit
$elapsed = 0
Write-Log ("waiting for Hermes desktop to exit (timeout: " + $TIMEOUT_SECONDS + "s, poll: " + $POLL_SECONDS + "s)")

while ($true) {
    $procs = Get-HermesProcesses
    $hCount = $procs.Hermes.Count
    $nCount = $procs.NodeBackend.Count

    if ($hCount -eq 0 -and $nCount -eq 0) {
        Write-Log ("Hermes fully exited after " + $elapsed + "s")
        break
    }

    if ($elapsed -ge $TIMEOUT_SECONDS) {
        Write-Log ("FATAL: timeout (" + $TIMEOUT_SECONDS + "s) waiting for Hermes to exit")
        Write-Log ("  remaining Hermes.exe:  " + $hCount)
        Write-Log ("  remaining node.exe:    " + $nCount)
        if ($hCount -gt 0) {
            $procs.Hermes | Select-Object -First 5 | ForEach-Object {
                Write-Log ("    pid=" + $_.Id + " started=" + $_.StartTime)
            }
        }
        if ($nCount -gt 0) {
            $procs.NodeBackend | Select-Object -First 5 | ForEach-Object {
                Write-Log ("    pid=" + $_.Id + " path=" + $_.Path)
            }
        }
        exit 3
    }

    if ($elapsed -eq 0 -or ($elapsed % 5) -eq 0) {
        Write-Log ("  t=" + $elapsed + "s  waiting on " + $hCount + " Hermes + " + $nCount + " backend")
    }

    Start-Sleep -Seconds $POLL_SECONDS
    $elapsed += $POLL_SECONDS
}

# 3) extra grace period for the venv shim to release
Write-Log ("extra " + $GRACE_SECONDS + "s grace for venv shim to release")
Start-Sleep -Seconds $GRACE_SECONDS

# 4) clear any stale lock from prior failed attempts
$lockFile = Join-Path $HERMES_HOME '.hermes-update-in-progress'
if (Test-Path -LiteralPath $lockFile) {
    try {
        Remove-Item -LiteralPath $lockFile -Force
        Write-Log ("cleared stale lock: " + $lockFile)
    } catch {
        Write-Log ("WARNING: could not clear stale lock (" + $lockFile + "): " + $_)
    }
}

# 5) exec the real installer with the original args
Write-Log ("launching real installer: " + $REAL_INSTALLER + " " + $argSummary)
$proc = Start-Process -FilePath $REAL_INSTALLER `
                     -ArgumentList $args `
                     -PassThru `
                     -NoNewWindow `
                     -WorkingDirectory $HERMES_HOME
$proc.WaitForExit()
$exitCode = $proc.ExitCode
Write-Log ("real installer exited with code " + $exitCode)
exit $exitCode
