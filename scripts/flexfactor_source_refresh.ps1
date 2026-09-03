# Safely refresh a desktop checkout before its launcher asks any questions.
#
# Desktop shortcuts point at files inside a checkout. Without this preflight, a
# shortcut can keep running an obsolete launcher forever even after GitHub main
# has removed an old execution path. Only a clean, non-diverged main checkout
# is fast-forwarded; local work is never overwritten or merged automatically.

function Invoke-FlexFactorSourceRefresh {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [object[]]$ForwardedArgs = @()
    )

    if ($env:FLEXFACTOR_SKIP_SOURCE_REFRESH -eq "1") { return }
    if (-not (Test-Path (Join-Path $Repository ".git"))) { return }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Write-Host "[source] Git is unavailable; using the installed checkout." -ForegroundColor Yellow
        return
    }

    $origin = (& $git.Source -C $Repository remote get-url origin 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($origin)) { return }
    $normalizedOrigin = "$origin".Trim().TrimEnd("/").ToLowerInvariant()
    if ($normalizedOrigin -notin @(
        "https://github.com/buckeye7066/flexfactor.git",
        "https://github.com/buckeye7066/flexfactor"
    )) {
        Write-Host "[source] This is a fork; automatic upstream refresh was not attempted." -ForegroundColor DarkGray
        return
    }

    $branch = (& $git.Source -C $Repository branch --show-current 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or "$branch".Trim() -ne "main") {
        Write-Host "[source] Not on main; automatic refresh was not attempted." -ForegroundColor DarkGray
        return
    }

    & $git.Source -C $Repository fetch --quiet --no-tags origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] Could not check GitHub for an update; using the installed checkout." -ForegroundColor Yellow
        return
    }

    $head = (& $git.Source -C $Repository rev-parse HEAD 2>$null | Select-Object -First 1)
    $upstream = (& $git.Source -C $Repository rev-parse origin/main 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($head) -or
        [string]::IsNullOrWhiteSpace($upstream) -or "$head".Trim() -eq "$upstream".Trim()) {
        return
    }

    & $git.Source -C $Repository merge-base --is-ancestor HEAD origin/main 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] Local main has commits not on GitHub; it was not overwritten." -ForegroundColor Yellow
        Write-Host "[source] Update that checkout manually before relying on this launcher." -ForegroundColor Yellow
        return
    }

    $changes = @(& $git.Source -C $Repository status --porcelain 2>$null)
    if ($LASTEXITCODE -ne 0 -or $changes.Count -gt 0) {
        Write-Host "[source] Local files are modified; they were not overwritten." -ForegroundColor Yellow
        Write-Host "[source] Update that checkout manually before relying on this launcher." -ForegroundColor Yellow
        return
    }

    & $git.Source -C $Repository merge --ff-only --quiet origin/main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] The safe fast-forward failed; using the installed checkout." -ForegroundColor Yellow
        return
    }

    Write-Host "[source] Updated FlexFactor to GitHub main; restarting the refreshed launcher." -ForegroundColor Green
    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path $powershell)) {
        $powershell = (Get-Process -Id $PID).Path
    }
    & $powershell -NoProfile -ExecutionPolicy Bypass -File $LauncherPath @ForwardedArgs
    $childExit = $LASTEXITCODE
    exit $childExit
}
