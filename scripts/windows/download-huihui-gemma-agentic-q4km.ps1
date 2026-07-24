# Download Huihui-gemma-4-12B-agentic-fable5 Q4_K_M for llama hot-swap secondary.
# llama-turboquant -hf needs HTTPS rebuild; use curl to HF resolve URL instead.
param(
    [string]$OutDir = "H:\elt_data\gguf_models\mradermacher\Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF"
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$outFile = Join-Path $OutDir "Huihui-gemma-4-12B-agentic-fable5-abliterated.Q4_K_M.gguf"
$url = "https://huggingface.co/mradermacher/Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF/resolve/main/Huihui-gemma-4-12B-agentic-fable5-abliterated.Q4_K_M.gguf"
$expectedMin = 7GB

if ((Test-Path -LiteralPath $outFile) -and ((Get-Item -LiteralPath $outFile).Length -gt $expectedMin)) {
    Write-Host ("Already present: {0} ({1:N2} GB)" -f $outFile, ((Get-Item $outFile).Length / 1GB))
} else {
    Write-Host "Downloading $url"
    Write-Host " -> $outFile"
    & curl.exe -L --fail --retry 5 --retry-delay 3 -C - -o $outFile $url
    if ($LASTEXITCODE -ne 0) { throw "curl failed exit=$LASTEXITCODE" }
}

$size = (Get-Item -LiteralPath $outFile).Length
if ($size -lt $expectedMin) { throw "Download incomplete: $size bytes" }

$metaPath = Join-Path $env:USERPROFILE ".hermes\logs\llama-hf-download\hf-hub-huihui-latest.json"
New-Item -ItemType Directory -Path (Split-Path $metaPath) -Force | Out-Null
@{
    repo = "mradermacher/Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF"
    file = "Huihui-gemma-4-12B-agentic-fable5-abliterated.Q4_K_M.gguf"
    path = $outFile
    size_gb = [math]::Round($size / 1GB, 2)
    model_id = "Huihui-gemma-4-12B-agentic-fable5-Q4_K_M"
    hf_tag = "mradermacher/Huihui-gemma-4-12B-agentic-fable5-abliterated-GGUF:Q4_K_M"
} | ConvertTo-Json | Set-Content -LiteralPath $metaPath -Encoding UTF8

Write-Host ("DONE {0:N2} GB -> {1}" -f ($size / 1GB), $outFile)
Write-Host "meta=$metaPath"
