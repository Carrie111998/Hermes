#requires -Version 5.1
<#
.SYNOPSIS
    Breaks the Windows in-app Update race by waiting for the Hermes desktop GUI
    to fully exit before exec'ing the real installer.

.DESCRIPTION
    The desktop's applyUpdates() flow on Windows:
      1. spawnUpdaterProcess('hermes-setup.exe', ['--update','--branch','main'], ...)
      2. writeUpdateMarker with child.pid
      3. setTimeout(app.quit, UPDATE_HANDOFF_DWELL_MS)

    The spawned hermes-setup.exe is a Tauri app. Tauri re-execs the actual
    updater logic into a different OS process; the lock's PID does not match
    that inner process's std::process::id(), so the self-PID adoption check
    in update.rs:161-165 fails and the wrapper aborts.

    Closing Hermes manually first makes the loop go away (empirically verified).
    This wrapper inserts that "wait for Hermes to die" step programmatically.
    When the desktop spawns us, we wait for Hermes.exe to exit, clear the
    update-in-progress lock (if it's ours or stale), then exec the REAL
    installer (hermes-setup-real.exe) with the original args.

    We wait ONLY on Hermes.exe. The Python gateway backends (Hermes-managed
    node.exe) sometimes orphan after the GUI quits; the Tauri installer
    handles that itself, and waiting for them here would hang the wrapper
    for up to 60s on machines where the backends don't get reaped.

.NOTES
    Exit codes:
      0 - real installer succeeded
      2 - real installer missing (misconfiguration)
      3 - timeout waiting for Hermes.exe to exit
      4 - refused: live foreign update-in-progress lock (another update running)

    Properties:
      - Idempotent:    safe to invoke manually
      - Bounded:       30s timeout, 500ms poll
      - Non-destructive: the real installer is never moved or modified
      - Reversible:    delete the wrapper files and rename hermes-setup-real.exe
                       back to hermes-setup.exe to restore the stock state
#>

# NOTE: deliberately NOT using [CmdletBinding()] here. The wrapper receives
# arbitrary forward-passed args (--update, --branch <name>, etc.) from the
# desktop, and strict-mode parameter binding would reject them as "unknown
# parameter names". Plain script + $args is the correct shape.

$ErrorActionPreference = 'Stop'

# ---- configuration ----
$HERMES_HOME      = Join-Path $env:LOCALAPPDATA 'hermes'
$REAL_INSTALLER   = Join-Path $HERMES_HOME 'hermes-setup-real.exe'
$LOG_DIR          = Join-Path $HERMES_HOME 'logs'
$LOG_FILE         = Join-Path $LOG_DIR   'update-wrapper.log'
$LOCK_FILE        = Join-Path $HERMES_HOME '.hermes-update-in-progress'
$TIMEOUT_SECONDS  = 30    # Hermes.exe GUI close normally takes 1-5s; 30s covers AV slowdowns
$POLL_MS          = 500   # half-second poll so the user sees snappy progress
$HERMES_PROC_NAME = 'Hermes'   # Get-Process uses process name, no .exe on Windows

if (-not (Test-Path -LiteralPath $LOG_DIR)) {
    New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null
}

# ---- logging ----

# ANSI color codes for stdout; the log file always gets the plain [level] prefix
# so it stays greppable in editors and CI.
$Script:LogColors = @{
    info  = 'Cyan'
    ok    = 'Green'
    warn  = 'Yellow'
    error = 'Red'
}

function Write-Log {
    param(
        [Parameter(Mandatory, Position=0)] [string]$Message,
        [ValidateSet('info','ok','warn','error')] [string]$Level = 'info'
    )
    $ts   = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss.fffzzz')
    $line = ("{0}  [{1,-5}] {2}" -f $ts, $Level, $Message)
    Add-Content -LiteralPath $LOG_FILE -Value $line
    $color = $Script:LogColors[$Level]
    if ($Host.UI.SupportsVirtualTerminal -or $env:TERM) {
        Write-Host $line
    } else {
        Write-Host $line -ForegroundColor $color
    }
}

function Write-Banner {
    param([string]$Title)
    $bar = '=' * 72
    Write-Log $bar
    Write-Log $Title
    Write-Log $bar
}

# ---- process helpers ----

function Test-HermesRunning {
    $procs = @(Get-Process -Name $HERMES_PROC_NAME -ErrorAction SilentlyContinue)
    return ($procs.Count -gt 0)
}

function Get-HermesPidList {
    @(Get-Process -Name $HERMES_PROC_NAME -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.Id })
}

# ---- main ----

$argSummary = if ($args.Count -gt 0) { ($args -join ' ') } else { '(no args)' }

Write-Banner "Hermes update wrapper"
Write-Log ("args:     {0}" -f $argSummary)
Write-Log ("pid:      {0}" -f $PID)
Write-Log ("user:     {0}" -f $env:USERNAME)
Write-Log ("home:     {0}" -f $HERMES_HOME)
Write-Log ("timeout:  {0}s  poll: {1}ms" -f $TIMEOUT_SECONDS, $POLL_MS)
Write-Log ("installer: {0}" -f (Split-Path $REAL_INSTALLER -Leaf))

# 1) sanity: real installer must exist
if (-not (Test-Path -LiteralPath $REAL_INSTALLER)) {
    Write-Log ("real installer not found: {0}" -f $REAL_INSTALLER) -Level 'error'
    Write-Log "the wrapper is staged but hermes-setup-real.exe is missing" -Level 'error'
    Write-Log "fix: re-apply the wrapper, or restore hermes-setup-real.exe from backup" -Level 'error'
    exit 2
}

# 2) wait for the desktop GUI to exit
#
# Only Hermes.exe blocks the file replacement the Tauri installer needs to do.
# Hermes-managed Python gateway backends (node.exe) may orphan after the GUI
# quits -- the Tauri installer handles that itself, and waiting for them here
# would hang the wrapper on machines where the backends don't get reaped.
Write-Log ("waiting for Hermes.exe to exit (timeout: {0}s, poll: {1}ms)" -f $TIMEOUT_SECONDS, $POLL_MS)

$waitStart = Get-Date
$elapsed = 0
$lastLogged = -1
while ($true) {
    if (-not (Test-HermesRunning)) {
        $elapsedActual = [math]::Round(((Get-Date) - $waitStart).TotalSeconds, 1)
        Write-Log ("Hermes.exe exited after {0}s" -f $elapsedActual) -Level 'ok'
        break
    }

    if ($elapsed -ge $TIMEOUT_SECONDS) {
        $remaining = (Get-HermesPidList) -join ','
        Write-Log ("timeout ({0}s) waiting for Hermes.exe to exit; still running: [{1}]" -f $TIMEOUT_SECONDS, $remaining) -Level 'error'
        exit 3
    }

    # Poll at $POLL_MS for snappy detection, but only log once per whole second
    # to keep the log readable.
    if ($elapsed -ne $lastLogged) {
        Write-Log ("  t={0,2}s  waiting on Hermes.exe" -f $elapsed)
        $lastLogged = $elapsed
    }
    Start-Sleep -Milliseconds $POLL_MS
    $elapsed = [int][math]::Floor(((Get-Date) - $waitStart).TotalSeconds)
}

# 3) clear the lock only if it is ours or provably stale.
#
# The lock at .hermes-update-in-progress is main's cross-process update mutex
# (see apps/desktop/electron/update-marker.ts and the Rust updater at
# apps/bootstrap-installer/src-tauri/src/update.rs:265-268). The desktop
# writes it with the spawned updater's PID before it quits; with our wrapper
# in place, that PID is the parent cmd.exe that ran hermes-update-wrapper.cmd.
# A live lock owned by a different PID is almost certainly a parallel
# dashboard or terminal `hermes update` -- deleting it would let this install
# race with the live one and corrupt the checkout. So:
#   - Lock PID is in our process tree (us or parent cmd.exe) -> clear, it was ours.
#   - Lock PID is dead (no such process)                    -> clear, stale leftover.
#   - Lock PID is alive and not us                          -> REFUSE; exit 4.
#   - Lock is unreadable / no PID                           -> clear, treat as stale.
if (Test-Path -LiteralPath $LOCK_FILE) {
    $lockContent = $null
    try { $lockContent = Get-Content -LiteralPath $LOCK_FILE -Raw -ErrorAction Stop } catch {}

    $lockPid = $null
    if ($lockContent) {
        $firstLine = ($lockContent -split "`n")[0].Trim()
        if ($firstLine -match '^\d+$') {
            $lockPid = [int]$firstLine
        }
    }

    $clearLock = $false
    $reason = ''

    if (-not $lockPid) {
        $clearLock = $true
        $reason = 'unreadable lock format (no PID); treating as stale'
    } else {
        # Build the set of PIDs that count as 'ours': this powershell.exe (us)
        # and the parent cmd.exe that the desktop spawned to invoke us.
        $ourPids = @($PID)
        $ownProc = $null
        try {
            $ownProc = Get-CimInstance -ClassName Win32_Process -Filter ('ProcessId=' + $PID) -ErrorAction Stop
        } catch {}
        if ($ownProc -and $ownProc.ParentProcessId) {
            $ourPids += @([int]$ownProc.ParentProcessId)
        }

        $ownerAlive = $false
        $ownerProc = $null
        try { $ownerProc = Get-Process -Id $lockPid -ErrorAction Stop } catch {}
        if ($ownerProc) { $ownerAlive = $true }

        $isOurs = $ourPids -contains $lockPid
        if ($isOurs) {
            $clearLock = $true
            $ourPidsStr = ($ourPids -join ',')
            $reason = ('lock is owned by this handoff (pid=' + $lockPid + ' in our set [' + $ourPidsStr + '])')
        } elseif ($ownerAlive) {
            Write-Log ('refusing to delete live foreign lock at ' + $LOCK_FILE + '; owner pid=' + $lockPid + ' is alive and not part of this handoff. exiting so the live update can complete.') -Level 'warn'
            exit 4
        } else {
            $clearLock = $true
            $reason = ('stale lock; owner pid=' + $lockPid + ' is no longer alive')
        }
    }

    if ($clearLock) {
        try {
            Remove-Item -LiteralPath $LOCK_FILE -Force
            Write-Log ('cleared lock: ' + $LOCK_FILE + ' (' + $reason + ')') -Level 'ok'
        } catch {
            Write-Log ('could not clear lock (' + $LOCK_FILE + '): ' + $_) -Level 'warn'
        }
    }
}

# 4) exec the real installer with the original args
Write-Log ("launching real installer: {0} {1}" -f $REAL_INSTALLER, $argSummary)

$installStart = Get-Date
$proc = Start-Process -FilePath $REAL_INSTALLER `
                     -ArgumentList $args `
                     -PassThru `
                     -NoNewWindow `
                     -WorkingDirectory $HERMES_HOME
$proc.WaitForExit()
$installSeconds = [math]::Round(((Get-Date) - $installStart).TotalSeconds, 1)
$exitCode = $proc.ExitCode

if ($exitCode -eq 0) {
    Write-Log ("real installer completed in {0}s (exit 0)" -f $installSeconds) -Level 'ok'
} else {
    Write-Log ("real installer exited with code {0} after {1}s" -f $exitCode, $installSeconds) -Level 'error'
}
Write-Banner "done"
exit $exitCode
