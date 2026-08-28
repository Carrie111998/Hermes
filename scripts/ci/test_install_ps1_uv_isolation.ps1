# Behavioral test for install.ps1's managed-uv isolation helpers.
#
# Run:  pwsh -NoProfile -File scripts/ci/test_install_ps1_uv_isolation.ps1
#
# Not wired into the default CI lane -- the Linux runners have no PowerShell
# host. It runs on any machine with pwsh, and on a Windows runner if one is
# ever added.
#
# Same AST-lift methodology as test_install_ps1_path_migration.ps1: parses
# install.ps1, lifts the real function bodies, and exercises the shipped
# logic -- private managed path, tier-1 user-uv preference, legacy bin\uv
# migration -- for real.  These helpers have no registry calls, so they are
# lifted verbatim (0/0) and driven with a temp HERMES_HOME and a shadowed
# Get-Command for the PATH probe.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$installPs1 = Join-Path $PSScriptRoot '..' 'install.ps1' | Resolve-Path

function Find-InstallFunction {
    param([string]$Name)
    $parsed = [System.Management.Automation.Language.Parser]::ParseFile(
        $installPs1, [ref]$null, [ref]$null)
    return $parsed.Find({
        param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $n.Name -eq $Name
    }, $true)
}

function Get-RewrittenDefinition {
    param([string]$Name, [int]$ExpectedReads = 0, [int]$ExpectedWrites = 0)
    $fn = Find-InstallFunction -Name $Name
    if (-not $fn) {
        throw "$Name not found in $installPs1"
    }
    $definition = $fn.Extent.Text
    $reads  = ([regex]'\[Environment\]::GetEnvironmentVariable\("Path", "User"\)').Matches($definition).Count
    $writes = ([regex]'\[Environment\]::SetEnvironmentVariable\("Path", ([^,]+), "User"\)').Matches($definition).Count
    if ($reads -ne $ExpectedReads -or $writes -ne $ExpectedWrites) {
        throw "expected $ExpectedReads read(s) and $ExpectedWrites write(s) in ${Name}; found $reads read(s), $writes write(s). Update this harness."
    }
    return $definition
}

Invoke-Expression (Get-RewrittenDefinition -Name 'Get-ManagedUvPath')
Invoke-Expression (Get-RewrittenDefinition -Name 'Test-UserUvUsable')
Invoke-Expression (Get-RewrittenDefinition -Name 'Move-LegacyManagedUv')

# install.ps1's write helpers (failure paths only here).
function Write-Info    { Write-Host "[info]  $args" }
function Write-Success { Write-Host "[ok]    $args" }
function Write-Warn    { Write-Host "[warn]  $args" }

# Point $HermesHome (script scope — the lifted functions read it) at a
# temp dir.
$script:HermesHome = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-uv-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $script:HermesHome | Out-Null

# Shadowed Get-Command for the tier-1 PATH probe: returns a fake uv source,
# or $null when the test wants "no user uv".
$script:FakeUvSource = $null
function Get-Command {
    param($Name, $ErrorAction)
    if ($Name -eq 'uv' -and $script:FakeUvSource) {
        return [pscustomobject]@{ Source = $script:FakeUvSource }
    }
    return $null
}

$script:Failures = 0

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

function Assert-True {
    param([bool]$Actual, [string]$Name)
    if ($Actual) { Write-Host "  PASS  $Name" }
    else {
        Write-Host "  FAIL  $Name"
        $script:Failures++
    }
}

Write-Host "install.ps1 Get-ManagedUvPath (private location)"
$managed = Get-ManagedUvPath
Assert-Equal (Join-Path (Join-Path $script:HermesHome "uv") "uv.exe") $managed `
    'managed uv lives in the private uv\ dir, not bin\'

Write-Host ""
Write-Host "install.ps1 Test-UserUvUsable (tier-1 PATH probe)"

# No uv on PATH -> $null.
$script:FakeUvSource = $null
Assert-Equal $null (Test-UserUvUsable) 'no uv on PATH: not usable'

# A real user uv that runs --version is adopted read-only.
$userUv = Join-Path $script:HermesHome "user-uv.exe"
Set-Content -Path $userUv -Value "@echo off`r`necho uv 0.12.6" -Encoding Ascii
$script:FakeUvSource = $userUv
Assert-Equal $userUv (Test-UserUvUsable) 'user uv on PATH: adopted'

# A PATH hit that is Hermes' OWN legacy binary is NOT "user".
$legacy = Join-Path $script:HermesHome "bin\uv.exe"
New-Item -ItemType Directory -Force -Path (Split-Path $legacy -Parent) | Out-Null
Set-Content -Path $legacy -Value "@echo off`r`necho uv 0.1.2" -Encoding Ascii
$script:FakeUvSource = $legacy
Assert-Equal $null (Test-UserUvUsable) 'PATH hit at legacy bin\uv.exe is not user'

# A PATH hit that is Hermes' OWN managed binary is NOT "user".
New-Item -ItemType Directory -Force -Path "$script:HermesHome\uv" | Out-Null
Set-Content -Path $managed -Value "@echo off`r`necho uv 0.9.9" -Encoding Ascii
$script:FakeUvSource = $managed
Assert-Equal $null (Test-UserUvUsable) 'PATH hit at managed uv\uv.exe is not user'

Write-Host ""
Write-Host "install.ps1 Move-LegacyManagedUv (one-time migration)"

# Legacy bin\uv.exe -> private dir.
Remove-Item $managed -Force
$script:FakeUvSource = $null
Assert-True (Move-LegacyManagedUv) 'legacy bin\uv.exe moved'
Assert-True (Test-Path $managed) 'managed path exists after migration'
Assert-Equal $false (Test-Path $legacy) 'legacy path gone after migration'

# No-op when the managed binary already exists.
Set-Content -Path $legacy -Value "@echo off`r`necho uv 0.1.2" -Encoding Ascii
Assert-Equal $false (Move-LegacyManagedUv) 'no-op when managed already present'
Assert-Equal $true (Test-Path $legacy) 'legacy left in place when managed present'

# No-op when there is no legacy binary.
Remove-Item $legacy -Force
Assert-Equal $false (Move-LegacyManagedUv) 'no-op when no legacy binary'

# Cleanup
Remove-Item -Recurse -Force $script:HermesHome -ErrorAction SilentlyContinue

if ($script:Failures -gt 0) {
    Write-Host ""
    Write-Host "$script:Failures assertion(s) failed"
    exit 1
}

Write-Host ""
Write-Host "all assertions passed"
