# Behavioral tests for the ``browser`` install stage in scripts/install.ps1.
#
# Run from a PowerShell 7 prompt:
#
#   pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/tests/test-install-ps1-browser-stage.ps1
#
# Strategy: dot-source install.ps1 with -Manifest (which loads all functions
# then returns control via `exit 0` in the dot-sourced context), override the
# side-effect functions (Test-Node, Install-AgentBrowser), then invoke
# Stage-Browser directly and inspect $script:_StageSkippedReason.  This avoids
# spawning real npm installs or touching the system's Node installation while
# exercising the actual Stage-Browser worker code path.
#
# The source-text/regex tests that previously lived in
# tests/test_install_ps1_browser_stage.py were replaced by this suite per
# review on #67835: source-text assertions pin implementation shape, not
# behavior. A minimal Python test survives for the ASCII invariant only
# (genuinely host-independent, CI-valuable on Linux).

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path))
$installScript = Join-Path $repoRoot "scripts\install.ps1"

if (-not (Test-Path $installScript)) {
    throw "Could not locate install.ps1 at $installScript"
}

$failures = 0
function Assert-Equal {
    param([Parameter(Mandatory=$true)] $Expected,
          [Parameter(Mandatory=$true)] $Actual,
          [Parameter(Mandatory=$true)] [string]$Label)
    if ($Expected -ne $Actual) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        Write-Host "  expected: $Expected"
        Write-Host "  actual:   $Actual"
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}
function Assert-True {
    param([Parameter(Mandatory=$true)] $Condition,
          [Parameter(Mandatory=$true)] [string]$Label)
    if (-not $Condition) {
        Write-Host "FAIL: $Label" -ForegroundColor Red
        $script:failures++
    } else {
        Write-Host "OK: $Label" -ForegroundColor Green
    }
}

# Run Stage-Browser in an isolated child pwsh process by:
#   1. Setting $env:HERMES_HOME to a temp dir
#   2. Dot-sourcing install.ps1 -Manifest (loads all functions, then returns)
#   3. Applying function overrides (after install.ps1 define them, so our
#      overrides win)
#   4. Calling Stage-Browser directly
#   5. Printing $script:_StageSkippedReason via RESULT: prefix for the parent
#
# Returns a hashtable: { SkipReason (string, may be empty) }
function Invoke-StageBrowserTest {
    param(
        [string]$TestHome,
        [string[]]$Overrides   # function override lines inserted after dot-source
    )
    $lines = @(
        "`$ErrorActionPreference = 'Stop'"
        "`$env:HERMES_HOME = '$TestHome'"
        ". '$installScript' -Manifest 2>`$null | Out-Null"
    )
    if ($Overrides) {
        $lines += $Overrides
    }
    $lines += "`$script:_StageSkippedReason = `$null"
    $lines += "Stage-Browser"
    $lines += "Write-Output ('RESULT:' + (`$script:_StageSkippedReason -join '`n'))"

    $wrapper = $lines -join "`r`n"
    $wrapperFile = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-child-$(New-Guid).ps1"
    Set-Content -Path $wrapperFile -Value $wrapper -NoNewline
    try {
        $raw = & pwsh -NoProfile -ExecutionPolicy Bypass -File $wrapperFile 2>&1
        $resultLine = ($raw | Where-Object { $_ -match '^RESULT:' } | Select-Object -First 1)
        $reason = ''
        if ($resultLine) {
            $reason = ($resultLine -replace '^RESULT:', '')
        }
        return @{ SkipReason = $reason; RawOutput = $raw }
    } finally {
        Remove-Item $wrapperFile -Force -ErrorAction SilentlyContinue
    }
}

# -----------------------------------------------------------------------------
# Test 1: Stage-Browser soft-skips when Node.js is unavailable
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser skips when Node is unavailable --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t1-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $false; return $false }'
    )
    Assert-True ($r.SkipReason -match 'Node\.js not available') "Node-missing: reason mentions Node.js unavailability (got: $($r.SkipReason))"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 2: Stage-Browser soft-skips when browser is in disabled_toolsets
# (block-sequence YAML form).  Use a real Test-Node override so we don't
# hit the real system node, and let the real Get-HermesConfigDisabledToolsets
# parse our fixture config.yaml.
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser skips when browser is disabled in config.yaml (block form) --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t2-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $configContent = "agent:`r`n  max_turns: 60`r`n  disabled_toolsets:`r`n    - browser`r`n    - memory"
    Set-Content -Path (Join-Path $testHome 'config.yaml') -Value $configContent -NoNewline

    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }'
    )
    Assert-True ($r.SkipReason -match 'disabled.*config\.yaml') "Disabled-toolsets (block): reason mentions config.yaml disabled (got: $($r.SkipReason))"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 3: Stage-Browser skips when disabled_toolsets uses inline flow form
# [browser, memory]
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser skips when disabled_toolsets uses inline form --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t3-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $configContent = "agent:`r`n  disabled_toolsets: [browser, memory]"
    Set-Content -Path (Join-Path $testHome 'config.yaml') -Value $configContent -NoNewline

    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }'
    )
    Assert-True ($r.SkipReason -match 'disabled.*config\.yaml') "Disabled-toolsets (inline): reason mentions config.yaml (got: $($r.SkipReason))"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 4: Stage-Browser does NOT skip when browser is absent from
# disabled_toolsets.  Override Install-AgentBrowser to a no-op so we verify
# the guard passed Stage-Browser through to the install call without
# side effects.
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser proceeds when browser is NOT disabled --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t4-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $configContent = "agent:`r`n  disabled_toolsets:`r`n    - memory`r`n    - x_search"
    Set-Content -Path (Join-Path $testHome 'config.yaml') -Value $configContent -NoNewline

    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }',
        'function Install-AgentBrowser { param([switch]$SkipChromium) }'
    )
    Assert-True ($r.SkipReason -eq '') "Not-disabled: no skip reason (proceeds to Install-AgentBrowser) (got: '$($r.SkipReason)')"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 5: Stage-Browser proceeds when config.yaml does not exist (fresh install)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser proceeds when config.yaml is absent (fresh install) --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t5-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }',
        'function Install-AgentBrowser { param([switch]$SkipChromium) }'
    )
    Assert-True ($r.SkipReason -eq '') "No-config: no skip reason (proceeds to Install-AgentBrowser) (got: '$($r.SkipReason)')"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 6: Stage-Browser emits a skip reason when Install-AgentBrowser throws
# (npm failure path -- the core of the reviewer's concern: optional browser
# install must not abort the desktop Update flow)
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Stage-Browser skips (not throws) when npm install fails --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t6-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }',
        'function Install-AgentBrowser { param([switch]$SkipChromium); throw "npm install failed (exit 1)" }'
    )
    Assert-True ($r.SkipReason -match 'install failed') "Npm-failure: reason mentions install failure (got: $($r.SkipReason))"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 7: Get-HermesConfigDisabledToolsets handles a complex config.yaml
# with other agent keys before disabled_toolsets and other top-level keys after
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Get-HermesConfigDisabledToolsets parses complex config --"
$testHome = Join-Path ([System.IO.Path]::GetTempPath()) "hermes-browse-t7-$(New-Guid)"
New-Item -ItemType Directory -Force -Path $testHome | Out-Null
try {
    $configContent = @(
        'model:',
        '  default: anthropic/claude-opus-4.6',
        'agent:',
        '  max_turns: 60',
        '  reasoning_effort: medium',
        '  disabled_toolsets:',
        '    - browser',
        'model_providers:',
        '  openrouter:'
    ) -join "`r`n"
    Set-Content -Path (Join-Path $testHome 'config.yaml') -Value $configContent -NoNewline

    $r = Invoke-StageBrowserTest -TestHome $testHome -Overrides @(
        'function Test-Node { $script:HasNode = $true; return $true }'
    )
    Assert-True ($r.SkipReason -match 'disabled.*config\.yaml') "Complex-config: reason mentions config.yaml disabled (got: $($r.SkipReason))"
} finally {
    Remove-Item -Recurse -Force $testHome -ErrorAction SilentlyContinue
}

# -----------------------------------------------------------------------------
# Test 8: Manifest includes the browser stage with correct shape and ordering
# -----------------------------------------------------------------------------
Write-Host ""
Write-Host "-- Manifest includes browser stage --"
$manifestJson = & pwsh -NoProfile -ExecutionPolicy Bypass -File $installScript -Manifest
Assert-Equal -Expected 0 -Actual $LASTEXITCODE -Label "-Manifest exits 0 for browser stage test"

$manifest = $null
try {
    $manifest = $manifestJson | ConvertFrom-Json
} catch {
    Assert-True $false -Label "-Manifest parses as JSON for browser stage test"
}

if ($manifest) {
    $names = $manifest.stages | ForEach-Object { $_.name }
    Assert-True ($names -contains 'browser') "Manifest contains 'browser' stage"
    $browserStage = $manifest.stages | Where-Object { $_.name -eq 'browser' } | Select-Object -First 1
    if ($browserStage) {
        Assert-Equal -Expected $false -Actual $browserStage.needs_user_input -Label "browser stage needs_user_input=false"
        Assert-Equal -Expected 'install' -Actual $browserStage.category -Label "browser stage category=install"
    }
    Assert-True ($names.IndexOf('node') -lt $names.IndexOf('browser')) "browser appears after node in manifest"
    Assert-True ($names.IndexOf('browser') -lt $names.IndexOf('configure')) "browser appears before configure in manifest"
}

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
Write-Host ""
if ($script:failures -eq 0) {
    Write-Host "All browser-stage behavioral tests passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($script:failures) test(s) FAILED." -ForegroundColor Red
    exit 1
}
