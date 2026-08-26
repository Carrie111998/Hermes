# Real cross-process regression for install.ps1 update ownership.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$install = Join-Path $repo 'scripts\install.ps1'
$root = Join-Path ([System.IO.Path]::GetTempPath()) ("hermes-install-ownership-" + [Guid]::NewGuid().ToString('N'))
$firstOut = Join-Path $root 'first.out'
$firstErr = Join-Path $root 'first.err'
$secondOut = Join-Path $root 'second.out'
$secondErr = Join-Path $root 'second.err'
$thirdOut = Join-Path $root 'third.out'
$thirdErr = Join-Path $root 'third.err'
$staleAOut = Join-Path $root 'stale-a.out'
$staleAErr = Join-Path $root 'stale-a.err'
$staleBOut = Join-Path $root 'stale-b.out'
$staleBErr = Join-Path $root 'stale-b.err'
$marker = Join-Path $root '.hermes-update-in-progress'
$powershell = Join-Path $PSHOME 'powershell.exe'

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

function Start-Probe([int]$HoldSeconds, [string]$Stdout, [string]$Stderr) {
    $args = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $install,
        '-HermesHome', $root, '-InstallDir', (Join-Path $root 'hermes-agent'),
        '-SelfTestUpdateOwnership', '-SelfTestHoldSeconds', [string]$HoldSeconds,
        '-Json'
    )
    return Start-Process -FilePath $powershell -ArgumentList $args -PassThru `
        -RedirectStandardOutput $Stdout -RedirectStandardError $Stderr
}

function Invoke-Probe([string]$Stdout, [string]$Stderr) {
    $args = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $install,
        '-HermesHome', $root, '-InstallDir', (Join-Path $root 'hermes-agent'),
        '-SelfTestUpdateOwnership', '-Json'
    )
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $powershell @args 2>$Stderr
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorAction
    }
    @($output) | Set-Content -LiteralPath $Stdout -Encoding UTF8
    return $code
}

try {
    New-Item -ItemType Directory -Path $root -Force | Out-Null

    $first = Start-Probe 5 $firstOut $firstErr
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $marker) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 50
    }
    Assert-True (Test-Path -LiteralPath $marker) 'first installer did not publish ownership'

    $secondCode = Invoke-Probe $secondOut $secondErr
    Assert-True ($secondCode -eq 1) "overlapping installer exited $secondCode, expected 1"
    $secondJson = Get-Content -LiteralPath $secondOut -Raw | ConvertFrom-Json
    Assert-True (-not $secondJson.ok) 'overlapping installer unexpectedly acquired ownership'
    Assert-True ($secondJson.reason -match "PID $($first.Id)") 'refusal did not name the live owner PID'

    $first.WaitForExit()
    $firstJson = Get-Content -LiteralPath $firstOut -Raw | ConvertFrom-Json
    Assert-True ($firstJson.ok) 'first installer did not complete its ownership probe'
    Assert-True (-not (Test-Path -LiteralPath $marker)) 'completed owner did not remove its marker'

    $thirdCode = Invoke-Probe $thirdOut $thirdErr
    Assert-True ($thirdCode -eq 0) 'a later installer could not acquire after release'

    # Two processes discovering the same dead marker must serialize cleanup
    # and publication. Exactly one may enter; the stale loser must not delete
    # the fresh owner's marker and proceed unlocked.
    "4294967294`n$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())`n" |
        Set-Content -LiteralPath $marker -NoNewline -Encoding ASCII
    $staleA = Start-Probe 2 $staleAOut $staleAErr
    $staleB = Start-Probe 2 $staleBOut $staleBErr
    $staleA.WaitForExit()
    $staleB.WaitForExit()
    $staleResults = @(
        (Get-Content -LiteralPath $staleAOut -Raw | ConvertFrom-Json),
        (Get-Content -LiteralPath $staleBOut -Raw | ConvertFrom-Json)
    )
    $staleSummary = $staleResults | ConvertTo-Json -Compress
    Assert-True (@($staleResults | Where-Object { $_.ok }).Count -eq 1) "dead-marker reclaim owner count was not one: $staleSummary"
    Assert-True (@($staleResults | Where-Object { -not $_.ok }).Count -eq 1) "dead-marker reclaim refusal count was not one: $staleSummary"

    Write-Host 'INSTALL OWNERSHIP SELF-TEST: PASS'
} finally {
    if ($first -and -not $first.HasExited) { Stop-Process -Id $first.Id -Force -ErrorAction SilentlyContinue }
    if ($staleA -and -not $staleA.HasExited) { Stop-Process -Id $staleA.Id -Force -ErrorAction SilentlyContinue }
    if ($staleB -and -not $staleB.HasExited) { Stop-Process -Id $staleB.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
