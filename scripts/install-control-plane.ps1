# ============================================================================
# Hermes Control Plane installer (Windows / Docker Desktop)
# ============================================================================
# Installs MongoDB replica set + TLS CA for agent authorization.
#
# Usage:
#   .\scripts\install-control-plane.ps1
#   .\scripts\install-control-plane.ps1 -WithEnrollApi
#   .\scripts\install-control-plane.ps1 -Enroll home-pc
# ============================================================================

param(
    [switch]$WithEnrollApi,
    [string]$Enroll = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Cp = Join-Path $Root "deploy\control-plane"

Write-Host ""
Write-Host "Hermes Control Plane installer"
Write-Host "  MongoDB replica set + CA for agent X.509 auth"
Write-Host ""

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker is required (Docker Desktop)."
}
if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    Write-Host "WARNING: openssl not on PATH. Install OpenSSL or use Git Bash to run install-control-plane.sh"
    Write-Host "Attempting Git Bash path…"
    $bash = @(
        "$env:LOCALAPPDATA\hermes\git\bin\bash.exe",
        "C:\Program Files\Git\bin\bash.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $bash) { throw "bash/openssl required" }
    $args = @("scripts/install-control-plane.sh")
    if ($WithEnrollApi) { $args += "--with-enroll-api" }
    if ($Enroll) { $args += @("--enroll", $Enroll) }
    & $bash @args
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path (Join-Path $Cp "certs"), (Join-Path $Cp "bundles") | Out-Null

$envFile = Join-Path $Cp ".env"
if (-not (Test-Path $envFile)) {
    Copy-Item (Join-Path $Cp ".env.example") $envFile
    $rootPass = -join ((1..32) | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) })
    $token = -join ((1..48) | ForEach-Object { "{0:x}" -f (Get-Random -Max 16) })
    (Get-Content $envFile) `
        -replace '^MONGO_ROOT_PASSWORD=.*', "MONGO_ROOT_PASSWORD=$rootPass" `
        -replace '^HERMES_ENROLL_TOKEN=.*', "HERMES_ENROLL_TOKEN=$token" |
        Set-Content $envFile
    Write-Host "Wrote $envFile"
}

# Prefer bash for openssl scripts (gen-ca / init / enroll)
$bash = @(
    "$env:LOCALAPPDATA\hermes\git\bin\bash.exe",
    "C:\Program Files\Git\bin\bash.exe",
    (Get-Command bash -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not $bash) { throw "Git Bash (bash.exe) is required to generate certificates on Windows." }

Push-Location $Cp
try {
    & $bash "./scripts/gen-ca.sh"
    if ($WithEnrollApi) {
        docker compose --profile enroll up -d
    } else {
        docker compose up -d mongo1 mongo2 mongo3 orchestrator
    }
    & $bash "./scripts/init-replica.sh"
    if ($Enroll) {
        & $bash "./scripts/enroll-agent.sh" --name $Enroll
    }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Control plane is up."
Write-Host "Enroll agents:  bash deploy/control-plane/scripts/enroll-agent.sh --name <pc>"
Write-Host "Then run scripts/install-agent.ps1 on each PC with the bundle."
Write-Host ""
