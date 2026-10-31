<#
.SYNOPSIS
  Run the Gate F resource measurement scenarios (master prompt 3.6 and 11) through the Python harness.
  Ollama and the XODUS backend must be stopped first: the harness takes an idle baseline, then starts and stops both.
  Results (JSON and a Markdown summary) go to experiments\results\gate-f-<timestamp>\.
.EXAMPLE
  scripts\measure.ps1              # two full runs, about 40 minutes
  scripts\measure.ps1 -DryRun      # one short run to check the harness (not used for decisions)
#>
param([switch]$DryRun, [int]$Runs = 2)
$ErrorActionPreference = 'Stop'
$backend = Join-Path $PSScriptRoot '..\backend'
$args = @('run', 'python', (Join-Path $PSScriptRoot 'measure.py'), '--runs', $Runs)
if ($DryRun) { $args += '--dry-run' }
Push-Location $backend
try { & uv @args; exit $LASTEXITCODE } finally { Pop-Location }
