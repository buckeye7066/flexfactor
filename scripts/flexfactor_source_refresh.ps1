# User-requested source updates. Never stash, reset or force a checkout.
function Stop-FlexFactorSourceRefresh {
    param([string]$Message, [int]$Code = 5)
    Write-Host "[source] $Message" -ForegroundColor Red
    exit $Code
}

function Restart-FlexFactorLauncher {
    param([string]$LauncherPath, [object[]]$ForwardedArgs = @())
    $powershell = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = (Get-Process -Id $PID).Path
    }
    & $powershell -NoProfile -ExecutionPolicy Bypass -File $LauncherPath @ForwardedArgs
    exit $LASTEXITCODE
}

function Invoke-FlexFactorSourceRefresh {
    param([string]$Repository, [string]$LauncherPath, [object[]]$ForwardedArgs = @())
    if ($env:FLEXFACTOR_SKIP_SOURCE_REFRESH -eq '1') { return }
    . (Join-Path $Repository 'scripts\flexfactor_python.ps1')
    $marker = Join-Path $Repository '.git\flexfactor-refresh-needs-install'
    # An accepted update may need dependencies; retain the marker on failure.
    if (Test-Path -LiteralPath $marker) {
        Invoke-FlexFactorPython -Repo $Repository -PyArgs @('-m', 'pip', 'install', '--disable-pip-version-check', '-e', ($Repository + '[all]'))
        if ($LASTEXITCODE -ne 0) {
            Stop-FlexFactorSourceRefresh 'Updated source dependencies could not be prepared. Retry the launcher.' 4
        }
        Invoke-FlexFactorPython -Repo $Repository -PyArgs @('-m', 'pip', 'check')
        if ($LASTEXITCODE -ne 0) {
            Stop-FlexFactorSourceRefresh 'Updated source has incompatible dependencies. Review the pip check output.' 4
        }
        Remove-Item -LiteralPath $marker
    }
    # Interactive desktop launch only; automation continues using its installed code.
    if ([Console]::IsInputRedirected) { return }
    Invoke-FlexFactorPython -Repo $Repository -PyArgs @(
        (Join-Path $Repository 'source_app_update.py'), '--repo', 'buckeye7066/flexfactor',
        '--name', 'FlexFactor', '--prompt')
    if ($LASTEXITCODE -eq 20) {
        Stop-FlexFactorSourceRefresh 'Update is busy or could not finish. Retry after reviewing its message.' 20
    }
    if ($LASTEXITCODE -eq 10) {
        Set-Content -LiteralPath $marker -Value 'accepted source update: prepare dependencies' -Encoding Ascii
        Restart-FlexFactorLauncher -LauncherPath $LauncherPath -ForwardedArgs $ForwardedArgs
    }
}
