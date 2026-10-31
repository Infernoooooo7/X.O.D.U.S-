<#
.SYNOPSIS
  Start the Ollama server for XODUS, bound to loopback, with the settings from the
  master prompt (section 3.8). Environment variables are set for this process tree only;
  the user's global environment is never changed.
#>
param(
    [string]$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    [string]$LogDir = (Join-Path $PSScriptRoot '..\logs'),
    [int]$TimeoutSeconds = 30
)
$ErrorActionPreference = 'Stop'

if (-not (Test-Path $OllamaExe)) {
    Write-Error "Ollama not found at $OllamaExe. See README.md (Setup)."
    exit 1
}
if (Get-NetTCPConnection -State Listen -LocalPort 11434 -ErrorAction SilentlyContinue) {
    Write-Error "Something is already listening on port 11434. Stop it first (scripts\stop-ollama.ps1)."
    exit 1
}

$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MAX_LOADED_MODELS = '2'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_MAX_QUEUE = '8'
$env:OLLAMA_FLASH_ATTENTION = '1'
$env:OLLAMA_KV_CACHE_TYPE = 'q8_0'
$env:OLLAMA_KEEP_ALIVE = '10m'
$env:OLLAMA_NO_CLOUD = '1'
Remove-Item Env:OLLAMA_ORIGINS -ErrorAction SilentlyContinue
# Ollama runs the model in llama.cpp's llama-server, which by default keeps earlier prompts in system memory (limit
# 8192 MiB) and up to 32 context checkpoints. Measured in Gate F with qwen3.5:4b: the runner grew from 1.0 to 5.3 GiB over
# 26 requests. With these two settings it stayed at 1.1 GiB, with no measurable slowdown over a 6-turn conversation
# (experiments/gate-f/PROTOCOL.md, amendment 4). Ollama passes them on to llama-server. Values already set are kept.
if (-not $env:LLAMA_ARG_CACHE_RAM) { $env:LLAMA_ARG_CACHE_RAM = '0' }
if (-not $env:LLAMA_ARG_CTX_CHECKPOINTS) { $env:LLAMA_ARG_CTX_CHECKPOINTS = '2' }

New-Item -ItemType Directory -Force $LogDir | Out-Null
$proc = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $LogDir 'ollama.out.log') `
    -RedirectStandardError (Join-Path $LogDir 'ollama.err.log')

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        $v = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2
        Write-Output "Ollama $($v.version) listening on 127.0.0.1:11434 (pid $($proc.Id))"
        exit 0
    } catch { Start-Sleep -Milliseconds 300 }
}
Write-Error "Ollama did not respond within $TimeoutSeconds s. See $LogDir\ollama.err.log"
exit 1
