# Switch the active hot-swap model by issuing a tiny chat request (autoload + LRU unload).
# Requires start-llama-hotswap.ps1 already running on HERMES_LLAMA_PORT (default 8080).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\switch-llama-hotswap.ps1 -Model Huihui-gemma-4-12B-agentic-fable5-Q4_K_M
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\switch-llama-hotswap.ps1 -Model huihui
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\switch-llama-hotswap.ps1 -Model qwen

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "Qwen3.6-35B-A3B-Uncensored-IQ3_M",
        "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M",
        "primary",
        "secondary",
        "qwen",
        "gemma",
        "huihui",
        "agentic"
    )]
    [string]$Model,
    [string]$BaseUrl = "",
    [int]$TimeoutSec = 600
)

$ErrorActionPreference = "Stop"

$map = @{
    primary   = "Qwen3.6-35B-A3B-Uncensored-IQ3_M"
    qwen      = "Qwen3.6-35B-A3B-Uncensored-IQ3_M"
    secondary = "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M"
    gemma     = "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M"
    huihui    = "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M"
    agentic   = "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M"
}
$modelId = if ($map.ContainsKey($Model)) { $map[$Model] } else { $Model }

if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    $BaseUrl = if ($env:HERMES_LLAMA_BASE_URL) { $env:HERMES_LLAMA_BASE_URL } else { "http://127.0.0.1:8080/v1" }
}
$BaseUrl = $BaseUrl.TrimEnd("/")

$body = @{
    model = $modelId
    max_tokens = 8
    messages = @(
        @{ role = "user"; content = "ping" }
    )
} | ConvertTo-Json -Depth 5

Write-Host "Switching hot-swap model -> $modelId (may unload the other; timeout=${TimeoutSec}s)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
try {
    $resp = Invoke-RestMethod -Uri "$BaseUrl/chat/completions" -Method Post -Body $body -ContentType "application/json" -TimeoutSec $TimeoutSec
    $sw.Stop()
    $preview = ""
    if ($resp.choices -and $resp.choices[0].message.content) {
        $preview = [string]$resp.choices[0].message.content
        if ($preview.Length -gt 80) { $preview = $preview.Substring(0, 80) + "..." }
    }
    Write-Host ("OK model={0} elapsed={1:N1}s preview={2}" -f $modelId, $sw.Elapsed.TotalSeconds, $preview)
    $models = Invoke-RestMethod -Uri "$BaseUrl/models" -TimeoutSec 10
    $ids = @($models.data | ForEach-Object { "{0}={1}" -f $_.id, $_.status.value })
    Write-Host ("/v1/models: {0}" -f ($ids -join ", "))
} catch {
    $sw.Stop()
    throw "Hot-swap to '$modelId' failed after $([math]::Round($sw.Elapsed.TotalSeconds,1))s: $($_.Exception.Message)"
}
