# Start Go-based Hermes Desktop<->backend watchdog (operator-only; NOT agent-reachable).
param(
    [int]$IntervalSec = 20,
    [int]$FailThreshold = 2,
    [switch]$Once,
    [switch]$NoPrewarm,
    [switch]$NoTsnet,
    [string]$Listen = "127.0.0.1:9920",
    [string]$HermesRoot = "",
    [string]$HermesHome = "",
    [switch]$BuildIfMissing,
    [switch]$ForceRestart,
    # Bound go build so restart-hermes-stack never hangs on go mod tidy / network.
    [int]$BuildTimeoutSec = 180,
    # Default skip go test for operator start path (full test via Build-HermesGoWatchdog.ps1).
    [switch]$RunBuildTests,
    # Watchdog-managed hermes serve port (must stay outside reserved ops: 9119/9120/8787/9920).
    # Desktop connects via desktop-backend.json / HERMES_DESKTOP_REMOTE_*; default 9118.
    [int]$ManagedBackendPort = 9119
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = if ($HermesRoot) { $HermesRoot } else { (Resolve-Path (Join-Path $ScriptDir "..\..")).Path }
if (-not $HermesHome) { $HermesHome = Join-Path $env:USERPROFILE ".hermes" }

$Exe = Join-Path $ScriptDir "watchdog-go\dist\hermes-watchdog.exe"

function Invoke-GoWatchdogBuildBounded {
    param(
        [string]$BuildScript,
        [int]$TimeoutSec,
        [switch]$SkipTest
    )
    $argList = @()
    if ($SkipTest) { $argList += "-SkipTest" }
    Write-Host ("Building Go watchdog (timeout={0}s, SkipTest={1})..." -f $TimeoutSec, [bool]$SkipTest)
    $proc = Start-Process -FilePath "powershell.exe" `
        -ArgumentList (@("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BuildScript) + $argList) `
        -WorkingDirectory $ScriptDir `
        -PassThru `
        -WindowStyle Hidden
    if (-not $proc) {
        throw "Failed to start Build-HermesGoWatchdog.ps1"
    }
    $finished = $proc.WaitForExit($TimeoutSec * 1000)
    if (-not $finished) {
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
        # Also kill orphaned go children from the timed-out build.
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.CommandLine -and $_.CommandLine -match 'watchdog-go' -and $_.Name -match '^(go|compile|link)\.exe$'
        } | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
        throw "Go watchdog build timed out after ${TimeoutSec}s"
    }
    if ($proc.ExitCode -ne 0) {
        throw "Go watchdog build failed (exit $($proc.ExitCode))"
    }
}

if (-not (Test-Path -LiteralPath $Exe)) {
    if ($BuildIfMissing) {
        $buildScript = Join-Path $ScriptDir "Build-HermesGoWatchdog.ps1"
        try {
            Invoke-GoWatchdogBuildBounded -BuildScript $buildScript -TimeoutSec $BuildTimeoutSec -SkipTest:(-not $RunBuildTests)
        } catch {
            Write-Warning $_.Exception.Message
            Write-Warning "Skipping Go watchdog start — run Build-HermesGoWatchdog.ps1 manually when ready."
            exit 0
        }
        if (-not (Test-Path -LiteralPath $Exe)) {
            Write-Warning "Build finished but missing $Exe — skipping Go watchdog start."
            exit 0
        }
    } else {
        throw "Missing $Exe — run Build-HermesGoWatchdog.ps1 first or pass -BuildIfMissing"
    }
}

$DataDir = Join-Path $env:LOCALAPPDATA "HermesWatchdog"
$LockPath = Join-Path $DataDir "watchdog.lock"

function Test-GoWatchdogAlive {
    if (-not (Test-Path -LiteralPath $LockPath)) { return $false }
    try {
        $obj = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
        $pidLock = [int]$obj.pid
        if ($pidLock -le 0) { return $false }
        $proc = Get-Process -Id $pidLock -ErrorAction SilentlyContinue
        return [bool]$proc
    } catch {
        return $false
    }
}

function Stop-GoWatchdog {
    if (Test-GoWatchdogAlive) {
        try {
            $obj = Get-Content -LiteralPath $LockPath -Raw | ConvertFrom-Json
            Stop-Process -Id ([int]$obj.pid) -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        } catch {}
    }
    Get-Process -Name hermes-watchdog -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
}

function Stop-PsDesktopBackendWatchdog {
    # PS and Go watchdogs use different lock files — running both causes dual
    # Hermes.exe relaunch loops. Prefer Go; stop the legacy PS mutual watchdog.
    $psLock = Join-Path $HermesHome "logs\desktop-backend-watchdog.lock"
    if (Test-Path -LiteralPath $psLock) {
        try {
            $obj = Get-Content -LiteralPath $psLock -Raw | ConvertFrom-Json
            if ($obj.pid) {
                Stop-Process -Id ([int]$obj.pid) -Force -ErrorAction SilentlyContinue
            }
        } catch {}
        Remove-Item -LiteralPath $psLock -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -match 'Start-HermesDesktopBackendWatchdog\.ps1'
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

Stop-PsDesktopBackendWatchdog

if ($ForceRestart -or $Once) {
    Stop-GoWatchdog
} elseif (Test-GoWatchdogAlive) {
    Write-Host "Go watchdog already running (lock=$LockPath)"
    exit 0
}

# Quote values with whitespace for the UseShellExecute fallback only.
function Format-WatchdogArg([string]$Name, [string]$Value) {
    if ($null -eq $Value) { $Value = "" }
    if ($Value -match '[\s"]') {
        $escaped = $Value.Replace('"', '\"')
        return ('{0}="{1}"' -f $Name, $escaped)
    }
    return ('{0}={1}' -f $Name, $Value)
}

# Pass flag name and value as separate argv entries so paths with spaces
# ("New project") are not truncated by Start-Process command-line quoting.
# Go's flag package accepts both -name=value and -name value.
$argList = @(
    "-interval=$IntervalSec",
    "-fail-threshold=$FailThreshold",
    "-hermes-root", $RepoRoot,
    "-hermes-home", $HermesHome,
    "-listen=$Listen"
)
if ($Once) { $argList += "-once" }
if ($NoPrewarm) { $argList += "-prewarm-backend=false" }
if ($ManagedBackendPort -gt 0) { $argList += "-managed-backend-port=$ManagedBackendPort" }
if (-not $NoTsnet -and ($env:HERMES_WATCHDOG_TS_AUTHKEY -or $env:TS_AUTHKEY)) {
    $argList += "-tsnet"
}

$env:HERMES_HOME = $HermesHome
$workDir = Split-Path -Parent $Exe
Write-Host "Starting Go watchdog detached: $Exe $($argList -join ' ')"

$launched = $false
try {
    $proc = Start-Process -FilePath $Exe -ArgumentList $argList -WorkingDirectory $workDir -WindowStyle Hidden -PassThru
    if ($proc) { $launched = $true }
} catch {
    Write-Warning "Start-Process ArgumentList failed: $($_.Exception.Message); trying UseShellExecute"
}
if (-not $launched) {
    # ShellExecute fallback: quote only values that contain whitespace.
    $shellArgs = @(
        "-interval=$IntervalSec",
        "-fail-threshold=$FailThreshold",
        (Format-WatchdogArg "-hermes-root" $RepoRoot),
        (Format-WatchdogArg "-hermes-home" $HermesHome),
        "-listen=$Listen"
    )
    if ($Once) { $shellArgs += "-once" }
    if ($NoPrewarm) { $shellArgs += "-prewarm-backend=false" }
    if ($ManagedBackendPort -gt 0) { $shellArgs += "-managed-backend-port=$ManagedBackendPort" }
    if (-not $NoTsnet -and ($env:HERMES_WATCHDOG_TS_AUTHKEY -or $env:TS_AUTHKEY)) {
        $shellArgs += "-tsnet"
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Exe
    $startInfo.WorkingDirectory = $workDir
    $startInfo.Arguments = ($shellArgs -join ' ')
    $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.UseShellExecute = $true
    [void][System.Diagnostics.Process]::Start($startInfo)
}

Start-Sleep -Seconds 2
if (Test-GoWatchdogAlive) {
    Write-Host "Go watchdog launched (logs: $(Join-Path $HermesHome 'logs\hermes-go-watchdog.log'))"
} else {
    Write-Warning "Go watchdog may still be starting — check $(Join-Path $HermesHome 'logs\hermes-go-watchdog.log')"
}
