<#>
.SYNOPSIS
    Comprehensive Tailscale Serve management for Hermes services.
    Idempotently configures all routes, validates them, and supports verification-only mode.

.DESCRIPTION
    This script replaces the fragmented Update-HermesTailscaleServe.ps1 with a single
    source of truth for all Tailscale Serve routes used by Hermes services.
    It is designed to be run at boot (via scheduled task) and on-demand for verification.

.PARAMETER Action
    Configure (default) - Create/update all routes and verify
    VerifyOnly - Only check current routes and health, don't modify
    Reset - Clear all routes (use with caution)

.PARAMETER Routes
    Hashtable of path -> target URL overrides. Defaults to standard Hermes routes.

.PARAMETER WaitSeconds
    Seconds to wait after configuring routes before verification (default 5).

.PARAMETER Verbose
    Show detailed output.

.EXAMPLE
    # Configure all standard routes at boot
    powershell -NoProfile -ExecutionPolicy Bypass -File Manage-HermesTailscaleServe.ps1

.EXAMPLE
    # Verify only (for health checks)
    powershell -NoProfile -ExecutionPolicy Bypass -File Manage-HermesTailscaleServe.ps1 -Action VerifyOnly

.EXAMPLE
    # Custom routes
    powershell ... -File Manage-HermesTailscaleServe.ps1 -Routes @{ "/custom" = "http://127.0.0.1:9999" }
#>

[CmdletBinding()]
param(
    [ValidateSet('Configure','VerifyOnly','Reset')]
    [string]$Action = 'Configure',

    [hashtable]$Routes = @{},

    [int]$WaitSeconds = 5,

    [switch]$ShowVerbose
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $prefix = switch ($Level) { 'WARN' {'[WARN] '} 'ERROR' {'[ERROR] '} default {''} }
    Write-Host "[$stamp] $prefix$Message"
}

function Get-TailscaleExe {
    $exe = Get-Command tailscale.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source
    if (-not $exe) { throw "tailscale.exe not found in PATH" }
    return $exe
}

function Get-DefaultRoutes {
    $routes = @{}
    $routes['/']                     = 'http://127.0.0.1:8787'
    $routes['/line']                 = 'http://127.0.0.1:8646/line'
    $routes['/v1']                   = 'http://127.0.0.1:8080/v1'
    $routes['/llama/v1']             = 'http://127.0.0.1:8081/v1'
    $routes['/hermes-gpt']           = 'http://127.0.0.1:7677'
    $routes['/world-intel']          = 'http://127.0.0.1:8501'
    $routes['/memory-graph']         = 'http://127.0.0.1:8765'
    $routes['/buzz']                 = 'http://127.0.0.1:3000'
    $routes['/freellmapi/v1']        = 'http://127.0.0.1:3001/v1'
    $routes['/personal-line-reply']  = 'http://127.0.0.1:9102/reply'
    $routes['/personal-line-health'] = 'http://127.0.0.1:9102/health'
    $routes['/cloudflare-os']        = 'http://127.0.0.1:8788'
    return $routes
}

function Get-TailscaleServeStatus {
    param($TailscaleExe)
    $json = & $TailscaleExe serve status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $json) { return @{} }
    try {
        # Write JSON to temp file and read back to avoid encoding issues
        $temp = [IO.Path]::GetTempFileName()
        [IO.File]::WriteAllText($temp, $json, [Text.Encoding]::UTF8)
        $obj = Get-Content $temp -Raw | ConvertFrom-Json
        Remove-Item $temp -Force -ErrorAction SilentlyContinue
        return $obj
    } catch {
        Write-Log "Failed to parse tailscale serve status: $($_.Exception.Message)" 'WARN'
        return @{}
    }
}

function Test-RouteConfigured {
    param($Status, [string]$Path, [string]$ExpectedTarget)
    if (-not $Status.Web) { return $false }
    # Web is a PSCustomObject with properties like "hostname:port"
    # Use PSObject.Properties to iterate safely
    foreach ($prop in $Status.Web.PSObject.Properties) {
        $web = $prop.Value
        $handlers = $web.Handlers
        if (-not $handlers) { continue }
        # handlers can be PSCustomObject or Hashtable
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

function Configure-Routes {
    param($TailscaleExe, [hashtable]$Routes)
    $changed = @()
    foreach ($path in $Routes.Keys | Sort-Object) {
        $target = $Routes[$path]
        Write-Log "Configuring route: $path -> $target"
        $result = & $TailscaleExe serve --bg --yes --set-path $path $target 2>&1
        if ($LASTEXITCODE -eq 0) {
            $changed += $path
            if ($ShowVerbose) { Write-Log "  OK: $path" }
        } else {
            Write-Log "  FAILED: $path -> $target`n  Output: $result" 'ERROR'
        }
    }
    return $changed
}

function Verify-Routes {
    param($TailscaleExe, [hashtable]$Routes)
    $status = Get-TailscaleServeStatus -TailscaleExe $TailscaleExe
    $results = @{}
    foreach ($path in $Routes.Keys | Sort-Object) {
        $target = $Routes[$path]
        $ok = Test-RouteConfigured -Status $status -Path $path -ExpectedTarget $target
        $results[$path] = @{ Configured = $ok; Target = $target }
        if ($ShowVerbose) {
            $state = if ($ok) { 'OK' } else { 'MISSING/MISMATCH' }
            Write-Log ("  " + $state + ": " + $path + " -> " + $target)
        }
    }
    return $results
}

function Test-LocalPort {
    param([int]$Port, [string]$Host = '127.0.0.1')
    $conn = Get-NetTCPConnection -LocalAddress $Host -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return $conn -ne $null
}

function Test-HttpHealth {
    param([string]$Url, [int]$TimeoutSec = 5)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -Method HEAD
        return $resp.StatusCode -ge 200 -and $resp.StatusCode -lt 400
    } catch {
        return $false
    }
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

# --- Main Execution ---

$tailscale = Get-TailscaleExe
$defaultRoutes = Get-DefaultRoutes
$effectiveRoutes = $defaultRoutes.Clone()
foreach ($key in $Routes.Keys) { $effectiveRoutes[$key] = $Routes[$key] }

Write-Log ("Tailscale Serve Manager - Action: " + $Action)
Write-Log ("Routes to manage: " + $effectiveRoutes.Count)

switch ($Action) {
    'Reset' {
        Write-Log "Resetting all Tailscale Serve routes..." 'WARN'
        & $tailscale serve reset 2>&1 | Out-Null
        Write-Log "Reset complete."
        exit 0
    }

    'VerifyOnly' {
        $results = Verify-Routes -TailscaleExe $tailscale -Routes $effectiveRoutes
        $allOk = $true
        foreach ($path in $results.Keys) {
            if (-not $results[$path].Configured) { $allOk = $false }
        }

        # Local port checks
        $portMap = @{}
        $portMap[8787] = '/ (WebUI)'
        $portMap[8646] = '/line (LINE webhook)'
        $portMap[8080] = '/v1 (Llama)'
        $portMap[8081] = '/llama/v1 (Alt Llama)'
        $portMap[7677] = '/hermes-gpt'
        $portMap[8501] = '/world-intel'
        $portMap[8765] = '/memory-graph'
        $portMap[3000] = '/buzz'
        $portMap[3001] = '/freellmapi/v1'
        $portMap[9102] = '/personal-line-* (Hakua Reply)'
        $portMap[8788] = '/cloudflare-os (dev)'
        $portResults = @{}
        foreach ($port in $portMap.Keys) {
            $listening = Test-LocalPort -Port $port
            $portResults[$port] = @{ Listening = $listening; Service = $portMap[$port] }
            if ($ShowVerbose) {
                $state = if ($listening) { 'LISTENING' } else { 'NOT LISTENING' }
                Write-Log ("  Port " + $port + " (" + $portMap[$port] + "): " + $state)
            }
        }

        # End-to-end HTTPS checks via Tailscale DNS
        $dns = Get-TailscaleDnsName -TailscaleExe $tailscale
        $httpsResults = @{}
        if ($dns) {
            $httpsPaths = @('/', '/personal-line-health', '/memory-graph/obsidian-memory-graph.html')
            foreach ($p in $httpsPaths) {
                $url = "https://$dns$p"
                $ok = Test-HttpHealth -Url $url -TimeoutSec 8
                $httpsResults[$p] = $ok
                if ($ShowVerbose) {
                    $resultText = if ($ok) { 'OK' } else { 'FAIL' }
                    Write-Log ("  HTTPS " + $url + ": " + $resultText)
                }
            }
        } else {
            Write-Log "Could not determine Tailscale DNS name; skipping HTTPS checks" 'WARN'
        }

        $summary = [PSCustomObject]@{
            Timestamp       = Get-Date -Format 'o'
            Action          = 'VerifyOnly'
            TailscaleDNSName    = $dns
            RoutesConfigured = $results
            LocalPorts      = $portResults
            HttpsHealth     = $httpsResults
            AllRoutesOk     = $allOk
        }
        $summary | ConvertTo-Json -Depth 5
        if ($allOk) { exit 0 } else { exit 1 }
    }

    'Configure' {
        # Configure routes
        $changed = Configure-Routes -TailscaleExe $tailscale -Routes $effectiveRoutes

        if ($WaitSeconds -gt 0) {
            Write-Log ("Waiting " + $WaitSeconds + " seconds for routes to propagate...")
            Start-Sleep -Seconds $WaitSeconds
        }

        # Verify
        $results = Verify-Routes -TailscaleExe $tailscale -Routes $effectiveRoutes
        $allOk = $true
        foreach ($path in $results.Keys) {
            if (-not $results[$path].Configured) { $allOk = $false }
        }

        # Output summary
        $summary = [PSCustomObject]@{
            Timestamp       = Get-Date -Format 'o'
            Action          = 'Configure'
            RoutesChanged   = $changed
            RoutesVerified  = $results
            AllRoutesOk     = $allOk
        }
        $summary | ConvertTo-Json -Depth 5
        if ($allOk) { exit 0 } else { exit 1 }
    }
}