# Behavioral test for install.ps1's User-PATH shim migration
# (Update-UserPathForHermes): drops the legacy venv\Scripts entry that
# hijacked the user's `python` command (#83797) and ensures the Hermes bin
# (shim) dir is present.
#
# Run:  pwsh -NoProfile -File scripts/ci/test_install_ps1_hermes_shim_path.ps1
#
# Not wired into the default CI lane — the Linux runners have no PowerShell
# host. It runs on any machine with pwsh (including via nixpkgs#powershell),
# and on a Windows runner if one is ever added.
#
# Same harness style as test_install_ps1_path_migration.ps1: parses
# install.ps1, lifts the real Update-UserPathForHermes body out of the AST,
# and rewrites *only* the two registry calls into an in-memory store so the
# actual shipped logic — split, drop-legacy, ensure-shim-dir, change-
# detection — executes for real.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$installPs1 = Join-Path $PSScriptRoot '..' 'install.ps1' | Resolve-Path
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $installPs1, [ref]$null, [ref]$null)

$fn = $ast.Find({
    param($n)
    $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Update-UserPathForHermes'
}, $true)

if (-not $fn) {
    throw "Update-UserPathForHermes not found in $installPs1"
}

# Swap the two registry calls for the in-memory store. Both must match, or the
# function has changed shape and this harness is no longer exercising it.
$definition = $fn.Extent.Text
$reads = ([regex]'\[Environment\]::GetEnvironmentVariable\("Path", "User"\)').Matches($definition).Count
$writes = ([regex]'\[Environment\]::SetEnvironmentVariable\("Path", ([^,]+), "User"\)').Matches($definition).Count
if ($reads -ne 1 -or $writes -ne 1) {
    throw "expected exactly one User PATH read and one write in the function body; found $reads read(s), $writes write(s). Update this harness."
}

$definition = $definition -replace `
    '\[Environment\]::GetEnvironmentVariable\("Path", "User"\)', '$script:FakeUserPath'
$definition = $definition -replace `
    '\[Environment\]::SetEnvironmentVariable\("Path", ([^,]+), "User"\)', '$script:FakeUserPath = $1; $script:FakeWrites++'

Invoke-Expression $definition

$BIN = 'C:\Users\me\AppData\Local\hermes\bin'
$VENV_SCRIPTS = 'C:\Users\me\AppData\Local\hermes\hermes-agent\venv\Scripts'
$script:Failures = 0

function Invoke-Migration {
    param([string]$Start, [string]$ShimDir = $BIN, [string]$Legacy = $VENV_SCRIPTS)
    $script:FakeUserPath = $Start
    $script:FakeWrites = 0
    Update-UserPathForHermes -ShimDir $ShimDir -LegacyVenvScripts $Legacy
}

function Assert-Equal {
    param($Expected, $Actual, [string]$Name)
    if ($Expected -ceq $Actual) {
        Write-Host "  PASS  $Name"
    } else {
        Write-Host "  FAIL  $Name"
        Write-Host "        expected: [$Expected]"
        Write-Host "        actual:   [$Actual]"
        $script:Failures++
    }
}

Write-Host "install.ps1 Update-UserPathForHermes"

# The regression this function exists for: installs made by the pre-2026-08
# installer, which prepended the venv Scripts dir (host of python.exe/pip.exe)
# to the persisted User PATH, hijacking `python` in every new shell.
Invoke-Migration "$VENV_SCRIPTS;C:\Program Files\nodejs;C:\Users\me\bin"
Assert-Equal "$BIN;C:\Program Files\nodejs;C:\Users\me\bin" $script:FakeUserPath `
    'upgrade from venv-Scripts installer: venv\Scripts dropped, shim dir prepended'
Assert-Equal 0 (@($script:FakeUserPath -split ';' | Where-Object { $_ -eq $VENV_SCRIPTS }).Count) `
    'upgrade: venv\Scripts fully removed'
Assert-Equal 1 (@($script:FakeUserPath -split ';' | Where-Object { $_ -eq $BIN }).Count) `
    'upgrade: shim dir present exactly once'
Assert-Equal 1 $script:FakeWrites 'upgrade: persists exactly once'

# A shim dir already at the tail (e.g. from an earlier partial install) stays
# put; only the stale venv\Scripts entry goes away.
Invoke-Migration "C:\Program Files\nodejs;$VENV_SCRIPTS;$BIN"
Assert-Equal "C:\Program Files\nodejs;$BIN" $script:FakeUserPath `
    'existing shim dir keeps its position, venv\Scripts removed'
Assert-Equal 1 $script:FakeWrites 'existing shim dir: persists exactly once'

Invoke-Migration "$BIN;C:\Program Files\nodejs"
Assert-Equal "$BIN;C:\Program Files\nodejs" $script:FakeUserPath 'already correct: unchanged'
Assert-Equal 0 $script:FakeWrites 'already correct: no registry write'

Invoke-Migration "C:\Program Files\nodejs"
Assert-Equal "$BIN;C:\Program Files\nodejs" $script:FakeUserPath 'fresh install: shim dir prepended'

# Empty segments are legal in a real User PATH (a trailing ';' is common) and
# the installer's other PATH code preserves them. Migration must not quietly
# rewrite parts of PATH it was not asked to touch.
Invoke-Migration "C:\Program Files\nodejs;;C:\Users\me\bin;"
Assert-Equal "$BIN;C:\Program Files\nodejs;;C:\Users\me\bin;" $script:FakeUserPath `
    'empty segments are preserved'

# Windows paths are case-insensitive.
Invoke-Migration "c:\users\me\appdata\local\hermes\hermes-agent\venv\scripts;C:\Program Files\nodejs"
Assert-Equal "$BIN;C:\Program Files\nodejs" $script:FakeUserPath `
    'legacy entry in different case is removed, not duplicated'

# A trailing backslash on the registry entry must not defeat the removal.
Invoke-Migration "$VENV_SCRIPTS\;C:\Program Files\nodejs"
Assert-Equal "$BIN;C:\Program Files\nodejs" $script:FakeUserPath `
    'trailing backslash variant is removed'

# Duplicate legacy entries all collapse.
Invoke-Migration "$VENV_SCRIPTS;C:\Program Files\nodejs;$VENV_SCRIPTS"
Assert-Equal "$BIN;C:\Program Files\nodejs" $script:FakeUserPath 'duplicates collapse'

Invoke-Migration ""
Assert-Equal $BIN $script:FakeUserPath 'empty User PATH'

# Empty LegacyVenvScripts (no-venv layout never calls this, but the guard
# must not filter unrelated entries when it is empty).
Invoke-Migration "C:\Program Files\nodejs;;" -Legacy ""
Assert-Equal "$BIN;C:\Program Files\nodejs;;" $script:FakeUserPath `
    'empty legacy arg: no filtering, empty segments kept'

Invoke-Migration "C:\Program Files\nodejs" "" ""
Assert-Equal "C:\Program Files\nodejs" $script:FakeUserPath 'empty ShimDir is a no-op'
Assert-Equal 0 $script:FakeWrites 'empty ShimDir does not write'

if ($script:Failures -gt 0) {
    Write-Host ""
    Write-Host "$script:Failures assertion(s) failed"
    exit 1
}

Write-Host ""
Write-Host "all assertions passed"
