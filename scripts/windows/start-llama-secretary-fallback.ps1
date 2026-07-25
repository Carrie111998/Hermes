# Fallback llama.cpp launcher — local Huihui agentic Q4_K_M on port 8081 by default.
# Do NOT default to NousResearch/Hermes-3-Llama-3.1-8B-GGUF:Q4_K_M — that HF :quant
# id is not a usable local asset on this host (cache stub / no weights), and
# llama-turboquant builds often lack HTTPS for -hf download.

param(
    [int]$WaitSeconds = 240
)

$ErrorActionPreference = "Stop"

function Resolve-Default {
    param([string]$Name, [string]$Default)
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($fromEnv)) { return $fromEnv }
    return $Default
}

function Resolve-FallbackGgufPath {
    $candidates = @(
        (Resolve-Default "HERMES_LLAMA_FALLBACK_GGUF_PATH" ""),
        "H:\elt_data\gguf_models\mradermacher\Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF\Huihui-gemma-4-12B-agentic-fable5-abliterated.Q4_K_M.gguf",
        "C:\Users\downl\Desktop\SO8T\gguf_models\Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-IQ3_M.gguf"
    )
    $metaPath = Join-Path $env:USERPROFILE ".hermes\logs\llama-hf-download\hf-hub-huihui-latest.json"
    if (Test-Path -LiteralPath $metaPath) {
        try {
            $meta = Get-Content -LiteralPath $metaPath -Raw -Encoding utf8 | ConvertFrom-Json
            if ($meta.path) { $candidates = @([string]$meta.path) + $candidates }
        } catch {}
    }
    foreach ($p in $candidates) {
        if ([string]::IsNullOrWhiteSpace($p)) { continue }
        if ((Test-Path -LiteralPath $p) -and -not ((Get-Item -LiteralPath $p).PSIsContainer) -and ((Get-Item -LiteralPath $p).Length -gt 1GB)) {
            return (Resolve-Path -LiteralPath $p).Path
        }
    }
    return ""
}

$ServerExe = Resolve-Default "HERMES_LLAMA_SERVER_EXE" (Join-Path $env:LOCALAPPDATA "Programs\llama-turboquant\bin\llama-server.exe")
$ModelPath = Resolve-FallbackGgufPath
$ModelRepo = Resolve-Default "HERMES_LLAMA_FALLBACK_MODEL" ""
$Alias = Resolve-Default "HERMES_LLAMA_FALLBACK_ALIAS" "huihui-gemma-agentic-fallback"
$HostName = Resolve-Default "HERMES_LLAMA_FALLBACK_HOST" "127.0.0.1"
$Port = [int](Resolve-Default "HERMES_LLAMA_FALLBACK_PORT" "8081")
$Ctx = [int](Resolve-Default "HERMES_LLAMA_FALLBACK_CTX" "65536")
$GpuLayers = [int](Resolve-Default "HERMES_LLAMA_FALLBACK_GPU_LAYERS" "99")

if ($Ctx -lt 64000) {
    throw "HERMES_LLAMA_FALLBACK_CTX=$Ctx is below minimum 64000."
}
if (-not (Test-Path -LiteralPath $ServerExe)) {
    throw "llama-server not found: $ServerExe"
}
if ([string]::IsNullOrWhiteSpace($ModelPath) -and [string]::IsNullOrWhiteSpace($ModelRepo)) {
    throw "No fallback GGUF found. Set HERMES_LLAMA_FALLBACK_GGUF_PATH or download Huihui Q4_K_M."
}
# Reject the known-bad default that only leaves an empty HF cache stub.
if ($ModelRepo -match '(?i)NousResearch/Hermes-3-Llama-3\.1-8B-GGUF') {
    Write-Warning "Ignoring unusable HERMES_LLAMA_FALLBACK_MODEL=$ModelRepo; prefer local GGUF."
    $ModelRepo = ""
    if ([string]::IsNullOrWhiteSpace($ModelPath)) {
        throw "Hermes-3 HF :Q4_K_M is not usable here. Point HERMES_LLAMA_FALLBACK_GGUF_PATH at a real .gguf."
    }
}

function Get-LlamaHelpText {
    param([string]$ServerExe)
    $output = & $ServerExe --help 2>&1 | Out-String
    return $output
}

function Test-HelpFlag {
    param([string]$HelpText, [string]$Pattern)
    return ($HelpText -match $Pattern)
}

$existing = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Listen" } |
    Select-Object -First 1
if ($existing) {
    Write-Output "llama.cpp fallback already listening on port $Port (pid=$($existing.OwningProcess))."
    exit 0
}

$logDir = Join-Path $env:USERPROFILE ".hermes\logs\llama-secretary-fallback"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$stdoutPath = Join-Path $logDir "fallback-$stamp.out.log"
$stderrPath = Join-Path $logDir "fallback-$stamp.err.log"
$helpText = Get-LlamaHelpText -ServerExe $ServerExe

$serverArgs = @(
    "--alias", $Alias,
    "--host", $HostName,
    "--port", [string]$Port,
    "--jinja",
    "-fa", "on",
    "-c", [string]$Ctx,
    "-ngl", [string]$GpuLayers,
    "-np", "1"
)

if (-not [string]::IsNullOrWhiteSpace($ModelPath)) {
    $serverArgs = @("-m", $ModelPath) + $serverArgs
    Write-Output "fallback model path=$ModelPath"
} else {
    $supportsHfRepoLong = Test-HelpFlag $helpText '--hf-repo'
    $supportsHfRepoShort = Test-HelpFlag $helpText '(^|[\s,])-hf([\s,]|$)'
    $supportsHfRepo = $supportsHfRepoLong -or $supportsHfRepoShort
    if (-not $supportsHfRepo) {
        throw "This llama-server build lacks -hf/--hf-repo; set HERMES_LLAMA_FALLBACK_GGUF_PATH to a local .gguf"
    }
    $hfFlag = if ($supportsHfRepoLong) { "--hf-repo" } else { "-hf" }
    $serverArgs = @($hfFlag, $ModelRepo) + $serverArgs
    Write-Output "fallback hf repo=$ModelRepo"
}

$process = Start-Process `
    -FilePath $ServerExe `
    -ArgumentList $serverArgs `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -WindowStyle Hidden `
    -PassThru

$modelsUrl = "http://${HostName}:${Port}/v1/models"
$deadline = (Get-Date).AddSeconds($WaitSeconds)
while ((Get-Date) -lt $deadline) {
    if ($process.HasExited) {
        $stderrTail = (Get-Content -LiteralPath $stderrPath -Tail 80 -ErrorAction SilentlyContinue) -join "`n"
        throw "fallback llama-server exited (exit=$($process.ExitCode)). stderr:`n$stderrTail"
    }
    try {
        $models = Invoke-RestMethod -Uri $modelsUrl -TimeoutSec 3
        Write-Output "llama.cpp fallback ready on $modelsUrl"
        Write-Output "pid=$($process.Id) alias=$Alias ctx=$Ctx"
        $models | ConvertTo-Json -Depth 6
        exit 0
    } catch {
        Start-Sleep -Seconds 2
    }
}

throw "Fallback llama-server did not become ready within $WaitSeconds seconds. stderr=$stderrPath"
