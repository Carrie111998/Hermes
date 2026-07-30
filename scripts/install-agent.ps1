# ============================================================================
# Hermes Agent installer (Windows PC) — Mongo remote mode
# ============================================================================
# Installs / wires Hermes on this PC with bootstrap.yaml + X.509 certs
# pointing at the control-plane MongoDB.
#
# Usage:
#   .\scripts\install-agent.ps1 -Bundle C:\path\to\home-pc
#   .\scripts\install-agent.ps1 -Bundle C:\path\to\home-pc.zip
#   .\scripts\install-agent.ps1 -EnrollUrl http://cp:8743/enroll -Token SECRET -Name home-pc
#   .\scripts\install-agent.ps1 -Bundle C:\bundles\home-pc -SkipHermesInstall
# ============================================================================

param(
    [string]$Bundle = "",
    [string]$EnrollUrl = "",
    [string]$Token = "",
    [string]$Name = "",
    [string]$Profile = "default",
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { "$env:LOCALAPPDATA\hermes" }),
    [switch]$SkipHermesInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host ""
Write-Host "Hermes Agent installer (PC)"
Write-Host "  HERMES_HOME=$HermesHome"
Write-Host ""

New-Item -ItemType Directory -Force -Path $HermesHome, (Join-Path $HermesHome "certs") | Out-Null

$work = $null
$bundleDir = $null

function Expand-Bundle([string]$path) {
    $tmp = Join-Path $env:TEMP ("hermes-bundle-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    if ($path -match '\.zip$') {
        Expand-Archive -Path $path -DestinationPath $tmp -Force
    } elseif ($path -match '\.tar\.gz$') {
        tar -xzf $path -C $tmp
    } else {
        throw "Unsupported archive: $path"
    }
    $dir = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $dir) { throw "Archive did not contain a bundle folder" }
    return @{ Work = $tmp; Dir = $dir.FullName }
}

if ($EnrollUrl) {
    if (-not $Token -or -not $Name) { throw "-EnrollUrl requires -Token and -Name" }
    $tmp = Join-Path $env:TEMP ("hermes-enroll-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $archive = Join-Path $tmp "bundle.tar.gz"
    Write-Host "Requesting enrollment from $EnrollUrl …"
    Invoke-RestMethod -Method Post -Uri $EnrollUrl `
        -Headers @{ Authorization = "Bearer $Token" } `
        -ContentType "application/json" `
        -Body (@{ name = $Name; profile = $Profile } | ConvertTo-Json) `
        -OutFile $archive
    $expanded = Expand-Bundle $archive
    $work = $expanded.Work
    $bundleDir = $expanded.Dir
} elseif ($Bundle) {
    if (Test-Path $Bundle -PathType Container) {
        $bundleDir = (Resolve-Path $Bundle).Path
    } elseif (Test-Path $Bundle -PathType Leaf) {
        $expanded = Expand-Bundle $Bundle
        $work = $expanded.Work
        $bundleDir = $expanded.Dir
    } else {
        throw "Bundle not found: $Bundle"
    }
} else {
    throw "Provide -Bundle or -EnrollUrl/-Token/-Name"
}

$bootSrc = Join-Path $bundleDir "bootstrap.yaml"
if (-not (Test-Path $bootSrc)) { throw "bootstrap.yaml missing in bundle" }

Write-Host "Installing bootstrap + certs into $HermesHome"
Copy-Item $bootSrc (Join-Path $HermesHome "bootstrap.yaml") -Force
$certsSrc = Join-Path $bundleDir "certs"
if (Test-Path $certsSrc) {
    Copy-Item (Join-Path $certsSrc "*") (Join-Path $HermesHome "certs") -Force
}

$env:HERMES_HOME = $HermesHome

if (-not $SkipHermesInstall) {
    $installPs1 = Join-Path $Root "scripts\install.ps1"
    if (Test-Path $installPs1) {
        Write-Host "Running scripts/install.ps1 -SkipSetup …"
        & $installPs1 -SkipSetup -HermesHome $HermesHome
    } else {
        Write-Host "install.ps1 not found — assuming hermes already installed."
    }
}

$hermes = Get-Command hermes -ErrorAction SilentlyContinue
if ($hermes) {
    Write-Host "hermes storage status"
    & hermes storage status
    & hermes cluster status
} else {
    Write-Host "NOTE: hermes not on PATH yet — open a new terminal, then:"
    Write-Host "  hermes storage status"
}

if ($work) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host "Agent PC configured for Mongo remote storage (X.509 preferred)."
Write-Host "Local files: bootstrap.yaml + certs\  only."
Write-Host ""
