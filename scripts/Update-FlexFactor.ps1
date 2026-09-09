# Keep manual updates on the same interpreter contract as desktop startup.
$ErrorActionPreference = 'Continue'
$Repository = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'flexfactor_python.ps1')
Invoke-FlexFactorPython -Repo $Repository -PyArgs @(
    (Join-Path $Repository 'source_app_update.py'), '--repo', 'buckeye7066/flexfactor',
    '--name', 'FlexFactor', '--prompt')
$updateExit = $LASTEXITCODE
if ($updateExit -eq 10) {
    Write-Host 'Update installed. Reopen FlexFactor through its normal launcher to prepare dependencies.'
    exit 0
}
exit $updateExit
