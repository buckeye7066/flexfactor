# Compatibility entry point. The former Glimmer-only bypass is retired because
# FlexFactor now has one model policy. This file forwards to the same ladder as
# every other launcher, so saved shortcuts do not become dead entry points.
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$launcherArguments = @($Rest)
. (Join-Path $PSScriptRoot 'scripts\flexfactor_source_refresh.ps1')
Invoke-FlexFactorSourceRefresh -Repository $PSScriptRoot `
    -LauncherPath $PSCommandPath -ForwardedArgs $launcherArguments

$script = Join-Path $PSScriptRoot "flexfactor_run.py"
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')

if (-not $Rest -or $Rest.Count -eq 0) {
    Write-Host "The Glimmer-only path is retired." -ForegroundColor Yellow
    Write-Host "Use: flexfactor_glimmer_launch.ps1 audit --program <repository>" -ForegroundColor Cyan
    Write-Host "The standard strongest-paid-to-free ladder will run." -ForegroundColor DarkGray
    exit 0
}

Write-Host "The Glimmer-only bypass is retired; using FlexFactor's standard model ladder." -ForegroundColor Cyan
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs (@($script) + $Rest + @("--model-mode", "best"))
exit $LASTEXITCODE
