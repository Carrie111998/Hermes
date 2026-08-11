<#>
.SYNOPSIS
    Comprehensive health check for Hermes services via Tailscale.
    Returns structured JSON suitable for monitoring and automation.

.DESCRIPTION
    Checks local ports, Tailscale Serve routes, and end-to-end HTTPS health.
    Exits 0 if all critical services are healthy, non-zero otherwise.

.PARAMETER CriticalOnly
    Only check critical services (WebUI, LINE, Memory Graph). Skip optional.

.PARAMETER Verbose
    Show detailed output.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File Test-HermesTailscaleHealth.ps1

.EXAMPLE
    powershell ... -File Test-HermesTailscaleHealth.ps1 -CriticalOnly
#>

[CmdletBinding()]
param(
    [switch]$CriticalOnly,
    [switch]$ShowVerbose
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $prefix = switch ($Level) { 'WARN' {'[WARN] '} 'ERROR' {'[ERROR] '} default {''} }
    if ($ShowVerbose -or $Level -ne 'INFO') { Write-Host "[$stamp] $prefix$Message" }
}

function Get-TailscaleExe {
    $exe = Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if (-not $exe) { throw "tailscale.exe not found in PATH" }
    return $exe
}

function Test-LocalPort {
    param([int]$Port, [string]$HostName = '127.0.0.1')
    try {
        $result = Test-NetConnection -ComputerName $HostName -Port $Port -WarningAction SilentlyContinue
        return $result.TcpTestSucceeded
    } catch {
        return $false
    }
}

function Test-HttpHealth {
    param([string]$Url, [int]$TimeoutSec = 10)
    try {
        # Try HEAD first, fall back to GET if HEAD fails (some endpoints don't support HEAD)
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -Method HEAD -ErrorAction Stop
            if ($resp -and $resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400) {
                return $true
            }
        } catch {
            # HEAD failed or returned error, try GET
        }
        # Fallback to GET
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -Method GET -ErrorAction Stop
        return $resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400
    } catch {
        return $false
    }
}

function Get-TailscaleServeStatus {
    param($TailscaleExe)
    $json = & $TailscaleExe serve status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return @{} }
    try {
        $temp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($temp, $json, [Text.Encoding]::UTF8)
        $obj = Get-Content $temp -Raw | ConvertFrom-Json
        Remove-Item $temp -Force -ErrorAction SilentlyContinue
        return $obj
    } catch { return @{} }
}

function Test-RouteConfigured {
    param($Status, [string]$Path, [string]$ExpectedTarget)
    if (-not $Status.Web) { return $false }
    foreach ($prop in $Status.Web.PSObject.Properties) {
        $web = $prop.Value
        $handlers = $web.Handlers
        if (-not $handlers) { continue }
        if ($handlers.PSObject.Properties) {
            foreach ($hprop in $handlers.PSObject.Properties) {
                if ($hprop.Name -eq $Path -and $hprop.Value.Proxy -eq $ExpectedTarget) {
                    return $true
                }
            }
        } elseif ($handlers[$Path] -and $handlers[$Path].Proxy -eq $ExpectedTarget) {
            return $true
        }
    }
    return $false
}

function Get-TailscaleDnsName {
    param($TailscaleExe)
    try {
        $json = & $TailscaleExe status --json 2>$null
        if ($json) {
            $temp = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($temp, $json, [Text.Encoding]::UTF8)
            $obj = Get-Content $temp -Raw | ConvertFrom-Json
            Remove-Item $temp -Force -ErrorAction SilentlyContinue
            if ($obj.Self -and $obj.Self.DNSName) {
                return [string]$obj.Self.DNSName | ForEach-Object { $_.TrimEnd('.') }
            }
        }
    } catch {}
    return $null
}

# --- Critical Services (Tailscale-accessible) ---
$criticalServices = @(
    @{ Name = 'Hermes WebUI'; Port = 8787; Path = '/'; Target = 'http://127.0.0.1:8787'; HttpsPath = '/health'; LocalOnly = $false },
    @{ Name = 'LINE Hakua Reply'; Port = 9102; Path = '/personal-line-reply'; Target = 'http://127.0.0.1:9102/reply'; HttpsPath = '/personal-line-health'; LocalOnly = $false },
    @{ Name = 'Memory Graph'; Port = 8765; Path = '/memory-graph'; Target = 'http://127.0.0.1:8765'; HttpsPath = '/memory-graph/obsidian-memory-graph.html'; LocalOnly = $false }
)

# --- Services requiring external tunnel (ngrok) ---
$tunneledServices = @(
    @{ Name = 'LINE Webhook'; Port = 8646; Path = '/line'; Target = 'http://127.0.0.1:8646/line'; HttpsPath = '/line'; LocalOnly = $false }
)

# --- Optional Services ---
$optionalServices = @(
    @{ Name = 'Llama API'; Port = 8080; Path = '/v1'; Target = 'http://127.0.0.1:8080/v1'; HttpsPath = '/v1/models'; LocalOnly = $false },
    @{ Name = 'Alt Llama API'; Port = 8081; Path = '/llama/v1'; Target = 'http://127.0.0.1:8081/v1'; HttpsPath = '/llama/v1/models'; LocalOnly = $false },
    @{ Name = 'Hermes GPT'; Port = 7677; Path = '/hermes-gpt'; Target = 'http://127.0.0.1:7677'; HttpsPath = '/hermes-gpt'; LocalOnly = $false },
    @{ Name = 'World Intel'; Port = 8501; Path = '/world-intel'; Target = 'http://127.0.0.1:8501'; HttpsPath = '/world-intel'; LocalOnly = $false },
    @{ Name = 'Buzz Relay'; Port = 3000; Path = '/buzz'; Target = 'http://127.0.0.1:3000'; HttpsPath = '/buzz'; LocalOnly = $false },
    @{ Name = 'FreeLLMAPI'; Port = 3001; Path = '/freellmapi/v1'; Target = 'http://127.0.0.1:3001/v1'; HttpsPath = '/freellmapi/v1/models'; LocalOnly = $false },
    @{ Name = 'Cloudflare OS (dev)'; Port = 8788; Path = '/cloudflare-os'; Target = 'http://127.0.0.1:8788'; HttpsPath = '/cloudflare-os/'; LocalOnly = $false }
)

$services = if ($CriticalOnly) { $criticalServices } else { $criticalServices + $tunneledServices + $optionalServices }

$tailscale = Get-TailscaleExe
$serveStatus = Get-TailscaleServeStatus -TailscaleExe $tailscale
$dnsName = Get-TailscaleDnsName -TailscaleExe $tailscale

$results = @()
$allCriticalOk = $true
$allOptionalOk = $true

foreach ($svc in $services) {
    $isCritical = $criticalServices.Contains($svc)
    $isTunneled = $tunneledServices.Contains($svc)
    $portOk = Test-LocalPort -Port $svc.Port
    $routeOk = Test-RouteConfigured -Status $serveStatus -Path $svc.Path -ExpectedTarget $svc.Target
    $httpsOk = $false
    if ($dnsName) {
        $url = "https://$dnsName$($svc.HttpsPath)"
        $timeout = if ($svc.Name -like '*Memory*' -or $svc.Name -like '*WebUI*') { 15 } else { 10 }
        $httpsOk = Test-HttpHealth -Url $url -TimeoutSec $timeout
    }

    # For tunneled services (ngrok), don't require local port listening
    if ($isTunneled) {
        $svcOk = $routeOk -and ($httpsOk -or -not $dnsName)
    } else {
        $svcOk = $portOk -and $routeOk -and ($httpsOk -or -not $dnsName)
    }
    if ($isCritical -and -not $svcOk) { $allCriticalOk = $false }
    if (-not $isCritical -and -not $isTunneled -and -not $svcOk) { $allOptionalOk = $false }

    $results += [PSCustomObject]@{
        Name         = $svc.Name
        Critical     = $isCritical
        Port         = $svc.Port
        PortListening = $portOk
        Route        = $svc.Path
        RouteTarget  = $svc.Target
        RouteConfigured = $routeOk
        HttpsUrl     = if ($dnsName) { "https://$dnsName$($svc.HttpsPath)" } else { 'N/A (no DNS)' }
        HttpsHealthy = $httpsOk
        OverallHealthy = $svcOk
    }

    if ($ShowVerbose) {
        Write-Log "$($svc.Name): Port=$([string]$portOk) Route=$([string]$routeOk) HTTPS=$([string]$httpsOk) Overall=$([string]$svcOk)"
    }
}

$summary = [PSCustomObject]@{
    Timestamp           = Get-Date -Format 'o'
    TailscaleDNSName    = $dnsName
    CriticalServicesOk  = $allCriticalOk
    OptionalServicesOk  = $allOptionalOk
    AllServicesOk       = $allCriticalOk -and $allOptionalOk
    Services            = $results
}

$summary | ConvertTo-Json -Depth 5

# Exit code: 0 if critical OK, 1 if critical failed, 2 if only optional failed
if (-not $allCriticalOk) { exit 1 }
if (-not $allOptionalOk) { exit 2 }
exit 0