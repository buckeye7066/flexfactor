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
        "https://github.com/buckeye7066/flexfactor",
        "git@github.com:buckeye7066/flexfactor.git",
        "git@github.com:buckeye7066/flexfactor",
        "ssh://git@github.com/buckeye7066/flexfactor.git",
        "ssh://git@github.com/buckeye7066/flexfactor"
    )) {
        Write-Host "[source] This is a fork; automatic upstream refresh was not attempted." -ForegroundColor DarkGray
        return
    }

    $branch = (& $git.Source -C $Repository branch --show-current 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or "$branch".Trim() -ne "main") {
        Write-Host "[source] Not on main; automatic refresh was not attempted." -ForegroundColor DarkGray
        return
    }

    $dependencyMarker = Join-Path $Repository ".git\flexfactor-refresh-needs-install"
    if (Test-Path $dependencyMarker) {
        $venvPython = Join-Path $Repository ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPython)) {
            Write-Host "[source] The updated source requires dependency reconciliation." -ForegroundColor Yellow
            Write-Host "[source] Run: py -3.12 -m venv .venv" -ForegroundColor Yellow
            Write-Host '[source] Then: .venv\Scripts\python.exe -m pip install -e ".[all]"' -ForegroundColor Yellow
            exit 4
        }
        $quotedRepo = '"' + $Repository.Replace('"', '\"') + '[all]"'
        $install = Start-Process -FilePath $venvPython -NoNewWindow -PassThru `
            -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-e", $quotedRepo)
        if (-not $install.WaitForExit(600000)) {
            Stop-Process -Id $install.Id -Force -ErrorAction SilentlyContinue
            Write-Host "[source] Dependency reconciliation timed out after 10 minutes." -ForegroundColor Red
            Write-Host '[source] Run: .venv\Scripts\python.exe -m pip install -e ".[all]"' -ForegroundColor Yellow
            exit 4
        }
        if ($install.ExitCode -ne 0) {
            Write-Host "[source] Dependency reconciliation failed with exit $($install.ExitCode)." -ForegroundColor Red
            Write-Host '[source] Run: .venv\Scripts\python.exe -m pip install -e ".[all]"' -ForegroundColor Yellow
            exit 4
        }
        Remove-Item -LiteralPath $dependencyMarker -Force
    }

    $quotedRepository = '"' + $Repository.Replace('"', '\"') + '"'
    $fetch = Start-Process -FilePath $git.Source -NoNewWindow -PassThru `
        -ArgumentList @("-C", $quotedRepository, "fetch", "--quiet", "--no-tags", "origin", "main")
    if (-not $fetch.WaitForExit(30000)) {
        Stop-Process -Id $fetch.Id -Force -ErrorAction SilentlyContinue
        Write-Host "[source] GitHub update check timed out after 30 seconds; using the installed checkout." -ForegroundColor Yellow
        return
    }
    if ($fetch.ExitCode -ne 0) {
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

    # `git status` intentionally hides ignored files. Git merge may replace an
    # ignored file when the incoming commit starts tracking that exact path, so
    # refuse such a collision before the fast-forward. `--no-renames` makes a
    # rename destination appear as an addition and therefore receive the same
    # protection. Case-insensitive Test-Path also covers Windows case drift.
    $incomingAdditions = @(& $git.Source -C $Repository diff --name-only `
        --no-renames --diff-filter=A HEAD origin/main 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] Could not inspect incoming paths; the checkout was not updated." -ForegroundColor Yellow
        return
    }
    foreach ($relativePath in $incomingAdditions) {
        if ([string]::IsNullOrWhiteSpace("$relativePath")) { continue }
        $candidate = Join-Path $Repository "$relativePath"
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        & $git.Source -C $Repository ls-files --error-unmatch -- "$relativePath" *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[source] Incoming tracked path would replace local ignored/untracked content: $relativePath" -ForegroundColor Red
            Write-Host "[source] The checkout was not updated." -ForegroundColor Yellow
            return
        }
    }

    $dependencyFiles = @(
        "pyproject.toml", "requirements.txt", "requirements-dev.txt",
        "setup.cfg", "setup.py"
    )
    $incomingChanges = @(& $git.Source -C $Repository diff --name-only HEAD origin/main 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] Could not inspect dependency changes; the checkout was not updated." -ForegroundColor Yellow
        return
    }
    $dependenciesChanged = @($incomingChanges | Where-Object { $_ -in $dependencyFiles }).Count -gt 0

    & $git.Source -C $Repository merge --ff-only --quiet origin/main
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[source] The safe fast-forward failed; using the installed checkout." -ForegroundColor Yellow
        return
    }
    if ($dependenciesChanged) {
        Set-Content -LiteralPath $dependencyMarker -Value "dependency metadata changed" -Encoding Ascii
        # Re-enter the refreshed helper so the new dependency graph is
        # reconciled before the updated source imports anything.
        Invoke-FlexFactorSourceRefresh -Repository $Repository `
            -LauncherPath $LauncherPath -ForwardedArgs $ForwardedArgs
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
