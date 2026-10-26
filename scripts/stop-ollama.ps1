<#
.SYNOPSIS
  Stop the Ollama server started by start-ollama.ps1, including its model runners
  (llama-server.exe). Only processes whose executable lives in the XODUS-configured Ollama
  folder are stopped; any other Ollama install is left alone.
#>
param([string]$OllamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe")

$dir = Split-Path -Parent (Resolve-Path $OllamaExe -ErrorAction SilentlyContinue).Path
if (-not $dir) { Write-Output "Ollama not found at $OllamaExe."; exit 0 }
$prefix = $dir.TrimEnd('\') + '\'
$procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
if (-not $procs) { Write-Output 'No XODUS-managed Ollama process is running.'; exit 0 }
# Server first, then any runner still alive.
$procs | Sort-Object { $_.ProcessName -ne 'ollama' } | ForEach-Object {
    Write-Output "Stopping $($_.ProcessName) pid $($_.Id)"
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}
