# Native Hermes fleet usage refresh — no WSL, pwsh 7+.
# Schedule below the capacity max_age (2h); default every 30 minutes.
param(
    [string]$HermesHome = $env:HERMES_HOME,
    [switch]$NoMirror,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

if (-not $HermesHome -or [string]::IsNullOrWhiteSpace($HermesHome)) {
    $HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
}

$env:HERMES_HOME = $HermesHome

function Resolve-Python {
    param([string]$Preferred)
    if ($Preferred -and (Test-Path -LiteralPath $Preferred)) {
        return $Preferred
    }
    foreach ($candidate in @(
        (Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"),
        (Join-Path $HermesHome "hermes-agent\.venv\Scripts\python.exe"),
        "python",
        "py"
    )) {
        try {
            if ($candidate -eq "py") {
                $ver = & py -3 -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) { return $ver.Trim() }
            } elseif ($candidate -eq "python") {
                $ver = & python -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) { return $ver.Trim() }
            } elseif (Test-Path -LiteralPath $candidate) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        } catch {
            continue
        }
    }
    throw "No Python interpreter found for fleet usage refresh"
}

$py = Resolve-Python -Preferred $Python
# Resolve only from this script's repository root or the installed Hermes home.
# Never fall back to a dated worktree path — those are ephemeral build checkouts.
$repoCandidates = @(
    (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue),
    (Join-Path $HermesHome "hermes-agent")
) | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $_ "hermes_cli\fleet\usage_refresh.py")) }

if (-not $repoCandidates) {
    throw "Could not locate hermes_cli.fleet.usage_refresh in known paths"
}
$repo = [string]$repoCandidates[0]
Set-Location -LiteralPath $repo

$argLine = "fleet refresh-usage --json"
if ($NoMirror) {
    $argLine = "$argLine --no-mirror"
}

Write-Host "[fleet-refresh-usage] HERMES_HOME=$HermesHome python=$py repo=$repo"
$env:PYTHONPATH = $repo
& $py -c "import runpy,sys; sys.argv=['hermes']+sys.argv[1:]; runpy.run_module('hermes_cli.main', run_name='__main__')" @($argLine.Split(' '))
$exit = $LASTEXITCODE
if ($exit -ne 0) {
    Write-Error "fleet refresh-usage failed with exit $exit"
    exit $exit
}
exit 0
