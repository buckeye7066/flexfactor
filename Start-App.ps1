# Canonical Windows app entry point.
# Generic desktop shortcut managers look for Start-App.ps1 before attempting
# package-manager heuristics. Keep the actual interactive product in
# flexfactor_launch.ps1 so every launch path shares the same model ladder and
# update policy.
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = "Stop"
$launcher = Join-Path $PSScriptRoot "flexfactor_launch.ps1"
if (-not (Test-Path -LiteralPath $launcher)) {
    Write-Error "FlexFactor launcher is missing: $launcher"
    exit 2
}

$powershell = Join-Path $PSHOME "powershell.exe"
if (-not (Test-Path -LiteralPath $powershell)) {
    $powershell = (Get-Process -Id $PID).Path
}

& $powershell -NoProfile -ExecutionPolicy Bypass -File $launcher @Rest
exit $LASTEXITCODE
