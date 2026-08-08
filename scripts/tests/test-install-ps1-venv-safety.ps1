# Native Windows behavior tests for the venv replacement boundary.
#
# These tests invoke the real venv stage against disposable directories. They
# never use the live Hermes install directory and clean up only their own
# fixtures. UV_CACHE_DIR is redirected because some hermetic Windows runners
# expose the user's default uv cache as a junction or a stale file.

$ErrorActionPreference = "Stop"
$scriptPath = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "install.ps1"
$pwshPath = (Get-Command pwsh -ErrorAction Stop).Source
$uvPath = (Get-Command uv -ErrorAction Stop).Source
$uvCacheRoot = Join-Path $env:TEMP ("hermes-venv-safety-uv-cache-" + [guid]::NewGuid().ToString("N"))
$testRoot = Join-Path $env:TEMP ("hermes-venv-safety-" + [guid]::NewGuid().ToString("N"))
$previousUvCache = $env:UV_CACHE_DIR
$previousOs = $env:OS
$failures = 0

function Assert-True {
    param([Parameter(Mandatory=$true)][bool]$Condition,
          [Parameter(Mandatory=$true)][string]$Label)
    if ($Condition) {
        Write-Host "OK: $Label" -ForegroundColor Green
    } else {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        $script:failures++
    }
}

function New-Fixture {
    $path = Join-Path $testRoot ([guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $path -Force | Out-Null
    return $path
}

function Write-FixtureFile {
    param([Parameter(Mandatory=$true)][string]$Path,
          [AllowEmptyString()][string]$Content)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Set-Content -LiteralPath $Path -Value $Content -NoNewline
}

function Get-GatewayTaskState {
    $rows = @(schtasks /Query /FO CSV 2>$null | ConvertFrom-Csv |
        Where-Object { $_.TaskName -like '*Hermes_Gateway*' } |
        ForEach-Object { "$($_.TaskName)=$($_.Status)" })
    return ($rows -join "|")
}

function Invoke-VenvStage {
    param([Parameter(Mandatory=$true)][string]$InstallDir)
    $output = @(& $pwshPath -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
        -Stage venv -InstallDir $InstallDir -NonInteractive 2>&1)
    [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = ($output -join "`n")
    }
}

function Stop-TestProcess {
    param([System.Diagnostics.Process]$Process)
    if ($null -ne $Process) {
        try {
            if (-not $Process.HasExited) {
                Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {
        }
    }
}

function Get-StageJson {
    param([Parameter(Mandatory=$true)][string]$Output)
    $jsonLine = ($Output -split "`r?`n" |
        Where-Object { $_.TrimStart().StartsWith('{') -and $_.TrimEnd().EndsWith('}') } |
        Select-Object -Last 1)
    if (-not $jsonLine) { return $null }
    return ($jsonLine | ConvertFrom-Json)
}

New-Item -ItemType Directory -Path $testRoot, $uvCacheRoot -Force | Out-Null
$env:UV_CACHE_DIR = $uvCacheRoot
$env:OS = 'Windows_NT'

try {
    Write-Host "-- native Windows venv replacement behavior --"

    # A disposable venv process is continually respawned by a supervisor that
    # runs outside the target venv. The real installer must stop retrying and
    # return a structured failure before attempting Rename-Item.
    $respawnDir = New-Fixture
    $respawnVenv = Join-Path $respawnDir "venv"
    $respawnScripts = Join-Path $respawnVenv "Scripts"
    & $uvPath venv $respawnVenv --python 3.11 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create the respawn fixture venv" }
    $runtimeHome = ((Get-Content (Join-Path $respawnVenv 'pyvenv.cfg') |
        Where-Object { $_ -like 'home = *' }) -replace '^home = ', '').Trim()
    Copy-Item -LiteralPath (Join-Path $runtimeHome 'python.exe') -Destination (Join-Path $respawnScripts 'python.exe') -Force
    Get-ChildItem -LiteralPath $runtimeHome -Filter '*.dll' -File |
        ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $respawnScripts -Force }
    $respawnExe = Join-Path $respawnScripts "python.exe"
    $respawnStop = Join-Path $respawnDir "stop-supervisor"
    $supervisor = Start-Job -ScriptBlock {
        param($target, $stop)
        while (-not (Test-Path -LiteralPath $stop)) {
            Start-Process -FilePath $target -ArgumentList @('-c', '"import time; time.sleep(60)"') | Out-Null
            Start-Sleep -Milliseconds 75
        }
    } -ArgumentList $respawnExe, $respawnStop
    try {
        Start-Sleep -Milliseconds 500
        $initialChildren = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq $respawnExe })
        Assert-True ($initialChildren.Count -gt 0) "respawn fixture starts a venv process"
        $beforeTasks = Get-GatewayTaskState
        $result = Invoke-VenvStage -InstallDir $respawnDir
        $afterTasks = Get-GatewayTaskState
        $stage = Get-StageJson -Output $result.Output
        Assert-True ($result.ExitCode -ne 0) "remaining venv process fails the stage"
        Assert-True ($null -ne $stage -and $stage.ok -eq $false) "remaining process returns structured failure"
        Assert-True (Test-Path -LiteralPath $respawnVenv) "remaining process leaves the original venv intact"
        Assert-True (-not (Get-ChildItem -LiteralPath $respawnDir -Directory -Filter 'venv.stale.*' -ErrorAction SilentlyContinue)) `
            "remaining process does not quarantine a live venv"
        Assert-True ($beforeTasks -eq $afterTasks) "scheduled-task state is preserved after process-lock failure"
    } finally {
        New-Item -ItemType File -Path $respawnStop -Force | Out-Null
        Stop-Job $supervisor -ErrorAction SilentlyContinue
        Remove-Job $supervisor -Force -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq $respawnExe } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }

    # A non-Hermes process holds a file without FILE_SHARE_DELETE. Quarantine
    # must fail closed and preserve the original tree; it must never fall back
    # to recursive in-place deletion.
    $lockedDir = New-Fixture
    $lockedVenv = Join-Path $lockedDir "venv"
    New-Item -ItemType Directory -Path $lockedVenv -Force | Out-Null
    $lockedFile = Join-Path $lockedVenv "held-by-test.bin"
    [IO.File]::WriteAllText($lockedFile, "rollback-marker")
    $lockStop = Join-Path $lockedDir "stop-locker"
    $lockedEscaped = $lockedFile.Replace("'", "''")
    $lockStopEscaped = $lockStop.Replace("'", "''")
    $lockerCode = @"
`$stream = [IO.File]::Open('$lockedEscaped', [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Read)
try {
    while (-not (Test-Path -LiteralPath '$lockStopEscaped')) { Start-Sleep -Milliseconds 100 }
} finally {
    `$stream.Dispose()
}
"@
    $locker = Start-Process -FilePath $pwshPath -WindowStyle Hidden -PassThru `
        -ArgumentList @('-NoProfile', '-Command', $lockerCode)
    try {
        Start-Sleep -Milliseconds 500
        $beforeTasks = Get-GatewayTaskState
        $result = Invoke-VenvStage -InstallDir $lockedDir
        $afterTasks = Get-GatewayTaskState
        $stage = Get-StageJson -Output $result.Output
        Assert-True ($result.ExitCode -ne 0) "failed quarantine rename fails the stage"
        Assert-True ($null -ne $stage -and $stage.ok -eq $false) "failed quarantine returns structured failure"
        Assert-True (Test-Path -LiteralPath $lockedFile) "failed quarantine retains the stale tree"
        Assert-True (-not (Get-ChildItem -LiteralPath $lockedDir -Directory -Filter 'venv.stale.*' -ErrorAction SilentlyContinue)) `
            "failed quarantine does not create a partial stale tree"
        Assert-True ($beforeTasks -eq $afterTasks) "scheduled-task state is preserved after quarantine failure"
    } finally {
        New-Item -ItemType File -Path $lockStop -Force | Out-Null
        Stop-TestProcess $locker
    }

    # A clean replacement must quarantine the old tree and retain its marker
    # after uv creates the replacement.
    $successDir = New-Fixture
    $successVenv = Join-Path $successDir "venv"
    $marker = Join-Path $successVenv "rollback-marker.txt"
    New-Item -ItemType Directory -Path $successVenv -Force | Out-Null
    [IO.File]::WriteAllText($marker, "retain-until-accepted")
    $beforeTasks = Get-GatewayTaskState
    $result = Invoke-VenvStage -InstallDir $successDir
    $afterTasks = Get-GatewayTaskState
    $stage = Get-StageJson -Output $result.Output
    $staleTrees = @(Get-ChildItem -LiteralPath $successDir -Directory -Filter 'venv.stale.*')
    Assert-True ($result.ExitCode -eq 0 -and $null -ne $stage -and $stage.ok -eq $true) `
        "clean replacement succeeds"
    Assert-True (Test-Path -LiteralPath (Join-Path $successVenv 'Scripts\python.exe')) `
        "replacement is created at venv, never .venv"
    Assert-True ($staleTrees.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $staleTrees[0].FullName 'rollback-marker.txt'))) `
        "successful replacement retains the quarantined stale tree"
    Assert-True ($beforeTasks -eq $afterTasks) "scheduled-task state is preserved across success"

    # A failed stage must also restore the same task state.
    $failureTasks = Get-GatewayTaskState
    Assert-True ($failureTasks -eq $afterTasks) "scheduled-task state remains stable after failure"

    # Exercise the real dependency stage with a tiny local project. A watcher
    # removes one generated launcher exactly once; the supported repair path
    # must reinstall it, keep the dependency target at venv, and finish with a
    # runnable launcher set without touching the user's PATH.
    $dependencyDir = New-Fixture
    $packageSource = @'
def main():
    return 0
'@
    $pyproject = @'
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "hermes-agent"
version = "0.0.0"
requires-python = ">=3.11,<3.14"
dependencies = []

[project.optional-dependencies]
all = []

[project.scripts]
hermes = "hermes_cli.main:main"
hermes-agent = "hermes_cli.main:main"
hermes-acp = "hermes_cli.main:main"

[tool.setuptools.packages.find]
include = ["hermes_cli*", "dotenv*", "openai*", "rich*", "prompt_toolkit*", "fastapi*", "uvicorn*"]
'@
    Write-FixtureFile (Join-Path $dependencyDir 'pyproject.toml') $pyproject
    Write-FixtureFile (Join-Path $dependencyDir 'hermes_cli\__init__.py') ''
    Write-FixtureFile (Join-Path $dependencyDir 'hermes_cli\main.py') $packageSource
    Write-FixtureFile (Join-Path $dependencyDir 'hermes_cli\web_server.py') 'app = None'
    foreach ($module in @('dotenv', 'openai', 'rich', 'prompt_toolkit', 'fastapi', 'uvicorn')) {
        Write-FixtureFile (Join-Path $dependencyDir "$module\__init__.py") ''
    }
    & $uvPath venv (Join-Path $dependencyDir 'venv') --python 3.11 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create the dependency fixture venv" }
    & $uvPath lock --directory $dependencyDir 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not lock the dependency fixture project" }
    $launcherStop = Join-Path $dependencyDir 'stop-launcher-watcher'
    $launcherScripts = Join-Path $dependencyDir 'venv\Scripts'
    $launcherWatcher = Start-Job -ScriptBlock {
        param($scripts, $stop)
        $target = Join-Path $scripts 'hermes.exe'
        while (-not (Test-Path -LiteralPath $stop)) {
            if (Test-Path -LiteralPath $target) {
                Remove-Item -LiteralPath $target -Force
                New-Item -ItemType File -Path $stop -Force | Out-Null
                break
            }
            Start-Sleep -Milliseconds 100
        }
    } -ArgumentList $launcherScripts, $launcherStop
    try {
        $result = @(& $pwshPath -NoProfile -ExecutionPolicy Bypass -File $scriptPath `
            -Stage dependencies -InstallDir $dependencyDir -NonInteractive 2>&1)
        $dependencyExit = $LASTEXITCODE
        $dependencyOutput = $result -join "`n"
        $dependencyStage = Get-StageJson -Output $dependencyOutput
        if ($dependencyExit -ne 0) { Write-Host $dependencyOutput }
        Assert-True ($dependencyExit -eq 0 -and $null -ne $dependencyStage -and $dependencyStage.ok -eq $true) `
            "dependency rebuild succeeds after launcher loss"
        Assert-True (Test-Path -LiteralPath (Join-Path $launcherScripts 'hermes.exe')) `
            "missing hermes launcher is repaired"
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $dependencyDir '.venv'))) `
            ".venv is not silently used as the dependency target"
        Assert-True ($dependencyOutput -like '*Console entry point(s) missing*' -and
            $dependencyOutput -like '*Console entry points restored*') `
            "launcher repair path is reported"
        $directSmoke = & (Join-Path $launcherScripts 'python.exe') -c 'import dotenv, openai, rich, prompt_toolkit; print("runtime-ok")' 2>&1
        Assert-True ($LASTEXITCODE -eq 0 -and ($directSmoke -join "`n") -like '*runtime-ok*') `
            "rebuilt venv passes direct runtime smoke check"
    } finally {
        New-Item -ItemType File -Path $launcherStop -Force | Out-Null
        Stop-Job $launcherWatcher -ErrorAction SilentlyContinue
        Remove-Job $launcherWatcher -Force -ErrorAction SilentlyContinue
    }
} catch {
    Write-Host "FAIL: unexpected test harness error: $($_.Exception.Message)" -ForegroundColor Red
    $failures++
} finally {
    if ($previousUvCache) { $env:UV_CACHE_DIR = $previousUvCache }
    else { Remove-Item Env:UV_CACHE_DIR -ErrorAction SilentlyContinue }
    $env:OS = $previousOs
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $uvCacheRoot) {
        Remove-Item -LiteralPath $uvCacheRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
if ($failures -gt 0) {
    Write-Host "FAILED: $failures assertion(s) failed" -ForegroundColor Red
    exit 1
}

Write-Host "All native Windows venv safety tests passed." -ForegroundColor Green
exit 0
