# Safely refresh a desktop checkout before its launcher asks any questions.
#
# Desktop shortcuts point at files inside a checkout. The runtime must never
# silently continue with a stale or locally modified FlexFactor tree. On the
# owner's canonical checkout we preserve local work first, bind the working
# tree to origin/main, reconcile dependency changes, and restart the launcher.

function Stop-FlexFactorSourceRefresh {
    param([Parameter(Mandatory = $true)][string]$Message, [int]$Code = 5)
    Write-Host "[source] $Message" -ForegroundColor Red
    Write-Host "[source] FlexFactor will not run from an unverified/stale checkout." -ForegroundColor Yellow
    exit $Code
}

function Restart-FlexFactorLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [object[]]$ForwardedArgs = @()
    )
    $powershell = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = (Get-Process -Id $PID).Path
    }
    & $powershell -NoProfile -ExecutionPolicy Bypass -File $LauncherPath @ForwardedArgs
    $childExit = $LASTEXITCODE
    exit $childExit
}

function Invoke-FlexFactorSourceRefresh {
    param(
        [Parameter(Mandatory = $true)][string]$Repository,
        [Parameter(Mandatory = $true)][string]$LauncherPath,
        [object[]]$ForwardedArgs = @()
    )

    # Git writes ordinary WARNINGS to stderr, and under the launcher's
    # $ErrorActionPreference = "Stop" a native command that writes ANY stderr
    # raises a terminating NativeCommandError -- even when the call is
    # redirected with *> $null. Measured: one untracked CRLF file (routine on
    # a core.autocrlf=true checkout) made `git stash push` warn, which killed
    # the launcher AFTER the stash had already moved the owner's uncommitted
    # work off disk -- so the work silently vanished, the "Preserved local
    # edits" disclosure never printed, and no program opened. This function
    # checks every exit status explicitly and never relies on Stop, so the
    # preference is narrowed here. The assignment is function-scoped and does
    # not change the launcher's own preference after the refresh returns.
    $ErrorActionPreference = "Continue"

    if ($env:FLEXFACTOR_SKIP_SOURCE_REFRESH -eq "1") { return }
    if (-not (Test-Path (Join-Path $Repository ".git"))) {
        Stop-FlexFactorSourceRefresh "The desktop launcher is not inside a Git checkout: $Repository"
    }

    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) {
        Stop-FlexFactorSourceRefresh "Git is unavailable, so current origin/main cannot be verified."
    }

    # Capture native output in full BEFORE narrowing it. `Select-Object -First`
    # tears the pipeline down as soon as it has its item, which kills git
    # mid-flight and leaves $LASTEXITCODE at -1 instead of git's real exit
    # status. Checking that -1 rejected a checkout that was already on clean
    # main, so the desktop shortcuts could not open FlexFactor at all.
    $originLines = @(& $git.Source -C $Repository remote get-url origin 2>$null)
    $originExit = $LASTEXITCODE
    $origin = $originLines | Select-Object -First 1
    if ($originExit -ne 0 -or [string]::IsNullOrWhiteSpace($origin)) {
        Stop-FlexFactorSourceRefresh "The checkout has no readable origin remote."
    }
    $normalizedOrigin = "$origin".Trim().TrimEnd("/").ToLowerInvariant()
    if ($normalizedOrigin -notin @(
        "https://github.com/buckeye7066/flexfactor.git",
        "https://github.com/buckeye7066/flexfactor",
        "git@github.com:buckeye7066/flexfactor.git",
        "git@github.com:buckeye7066/flexfactor",
        "ssh://git@github.com/buckeye7066/flexfactor.git",
        "ssh://git@github.com/buckeye7066/flexfactor"
    )) {
        Stop-FlexFactorSourceRefresh "This desktop checkout is not bound to buckeye7066/flexfactor origin."
    }

    $branchLines = @(& $git.Source -C $Repository branch --show-current 2>$null)
    $branchExit = $LASTEXITCODE
    $branch = $branchLines | Select-Object -First 1
    if ($branchExit -ne 0 -or "$branch".Trim() -ne "main") {
        Stop-FlexFactorSourceRefresh "The desktop checkout must be on main before it can run. Current branch: $branch"
    }

    $dependencyMarker = Join-Path $Repository ".git\flexfactor-refresh-needs-install"
    if (Test-Path $dependencyMarker) {
        $venvPython = Join-Path $Repository ".venv\Scripts\python.exe"
        if (-not (Test-Path $venvPython)) {
            Stop-FlexFactorSourceRefresh "The updated source requires dependency reconciliation, but .venv is missing." 4
        }
        $quotedRepo = '"' + $Repository.Replace('"', '\"') + '[all]"'
        $install = Start-Process -FilePath $venvPython -NoNewWindow -PassThru `
            -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "-e", $quotedRepo)
        # Retain the handle first, or the exit code below degrades to $null.
        $null = $install.Handle
        if (-not $install.WaitForExit(600000)) {
            Stop-Process -Id $install.Id -Force -ErrorAction SilentlyContinue
            Stop-FlexFactorSourceRefresh "Dependency reconciliation timed out after 10 minutes." 4
        }
        if ($install.ExitCode -ne 0) {
            Stop-FlexFactorSourceRefresh "Dependency reconciliation failed with exit $($install.ExitCode)." 4
        }
        Remove-Item -LiteralPath $dependencyMarker -Force
    }

    $quotedRepository = '"' + $Repository.Replace('"', '\"') + '"'
    $fetch = Start-Process -FilePath $git.Source -NoNewWindow -PassThru `
        -ArgumentList @("-C", $quotedRepository, "fetch", "--quiet", "--no-tags", "origin", "main")
    # Retaining the process handle is load-bearing. Start-Process -PassThru
    # hands back a Process object that has not cached the native handle, so
    # once the child exits the runtime cannot read it and the exit code
    # degrades to $null -- and $null -ne 0 is TRUE, so a healthy fetch read
    # as a failure and the desktop shortcut refused to launch.
    $null = $fetch.Handle
    if (-not $fetch.WaitForExit(30000)) {
        Stop-Process -Id $fetch.Id -Force -ErrorAction SilentlyContinue
        Stop-FlexFactorSourceRefresh "GitHub update check timed out after 30 seconds."
    }
    if ($fetch.ExitCode -ne 0) {
        Stop-FlexFactorSourceRefresh "Could not verify GitHub origin/main."
    }

    $headLines = @(& $git.Source -C $Repository rev-parse HEAD 2>$null)
    $headExit = $LASTEXITCODE
    $head = $headLines | Select-Object -First 1
    $upstreamLines = @(& $git.Source -C $Repository rev-parse origin/main 2>$null)
    $upstreamExit = $LASTEXITCODE
    $upstream = $upstreamLines | Select-Object -First 1
    if ($headExit -ne 0 -or $upstreamExit -ne 0 -or [string]::IsNullOrWhiteSpace($head) -or
        [string]::IsNullOrWhiteSpace($upstream)) {
        Stop-FlexFactorSourceRefresh "Could not resolve local HEAD and origin/main."
    }
    $head = "$head".Trim()
    $upstream = "$upstream".Trim()

    # IMPORTANT: inspect the working tree BEFORE the HEAD==origin/main shortcut.
    # A checkout can point at the exact current commit while flexfactor.py or a
    # launcher is locally modified. The previous ordering silently ran those
    # modified/stale bytes and is how a repaired defect could appear to return.
    $changes = @(& $git.Source -C $Repository status --porcelain --untracked-files=all 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-FlexFactorSourceRefresh "Could not inspect the working tree."
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $preserved = $false
    if ($changes.Count -gt 0) {
        $stashMessage = "FlexFactor auto-preserved before verified source refresh $stamp"
        & $git.Source -C $Repository stash push --include-untracked -m $stashMessage *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-FlexFactorSourceRefresh "Local work exists and could not be preserved with git stash."
        }
        $remaining = @(& $git.Source -C $Repository status --porcelain --untracked-files=all 2>$null)
        if ($LASTEXITCODE -ne 0 -or $remaining.Count -gt 0) {
            Stop-FlexFactorSourceRefresh "Local work was preserved incompletely; refusing to overwrite it."
        }
        $preserved = $true
        Write-Host "[source] Preserved local edits in git stash: $stashMessage" -ForegroundColor Yellow
    }

    $incomingChanges = @(& $git.Source -C $Repository diff --name-only HEAD origin/main 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-FlexFactorSourceRefresh "Could not inspect incoming source changes."
    }
    $dependencyFiles = @(
        "pyproject.toml", "requirements.txt", "requirements-dev.txt",
        "setup.cfg", "setup.py"
    )
    $dependenciesChanged = @($incomingChanges | Where-Object { $_ -in $dependencyFiles }).Count -gt 0

    # Protect local commits too. If main diverged/ahead, anchor the old HEAD on
    # a rescue branch before resetting the executable checkout to authoritative
    # origin/main. Nothing is discarded; it simply stops being executable main.
    & $git.Source -C $Repository merge-base --is-ancestor HEAD origin/main 2>$null
    $fastForwardable = ($LASTEXITCODE -eq 0)
    if (-not $fastForwardable -and $head -ne $upstream) {
        $rescue = "flexfactor/local-preserved-$stamp-$PID"
        & $git.Source -C $Repository branch $rescue HEAD 2>$null
        if ($LASTEXITCODE -ne 0) {
            Stop-FlexFactorSourceRefresh "Local commits differ from origin/main and could not be preserved on a rescue branch."
        }
        Write-Host "[source] Preserved local commits on branch: $rescue" -ForegroundColor Yellow
    }

    # `git status` intentionally hides ignored files. Git may replace an
    # ignored path if origin/main begins tracking that exact name, so detect
    # that collision before updating. Untracked files were already stashed.
    $incomingAdditions = @(& $git.Source -C $Repository diff --name-only `
        --no-renames --diff-filter=A HEAD origin/main 2>$null)
    if ($LASTEXITCODE -ne 0) {
        Stop-FlexFactorSourceRefresh "Could not inspect incoming paths."
    }
    foreach ($relativePath in $incomingAdditions) {
        if ([string]::IsNullOrWhiteSpace("$relativePath")) { continue }
        $candidate = Join-Path $Repository "$relativePath"
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        & $git.Source -C $Repository ls-files --error-unmatch -- "$relativePath" *> $null
        if ($LASTEXITCODE -ne 0) {
            Stop-FlexFactorSourceRefresh "Incoming tracked path would replace ignored local content: $relativePath"
        }
    }

    $sourceChanged = $preserved -or ($head -ne $upstream)
    if ($head -ne $upstream) {
        if ($fastForwardable) {
            & $git.Source -C $Repository merge --ff-only --quiet origin/main
            if ($LASTEXITCODE -ne 0) {
                Stop-FlexFactorSourceRefresh "The verified fast-forward to origin/main failed."
            }
        } else {
            & $git.Source -C $Repository reset --hard origin/main *> $null
            if ($LASTEXITCODE -ne 0) {
                Stop-FlexFactorSourceRefresh "Could not reset the executable checkout to preserved origin/main."
            }
        }
    }

    $finalHeadLines = @(& $git.Source -C $Repository rev-parse HEAD 2>$null)
    $finalHeadExit = $LASTEXITCODE
    $finalHead = $finalHeadLines | Select-Object -First 1
    $finalStatus = @(& $git.Source -C $Repository status --porcelain --untracked-files=all 2>$null)
    if ($finalHeadExit -ne 0 -or $LASTEXITCODE -ne 0 -or "$finalHead".Trim() -ne $upstream -or $finalStatus.Count -gt 0) {
        Stop-FlexFactorSourceRefresh "Post-refresh verification failed; executable source is not an exact clean origin/main tree."
    }

    if ($dependenciesChanged) {
        Set-Content -LiteralPath $dependencyMarker -Value "dependency metadata changed" -Encoding Ascii
        Invoke-FlexFactorSourceRefresh -Repository $Repository `
            -LauncherPath $LauncherPath -ForwardedArgs $ForwardedArgs
    }

    if ($sourceChanged) {
        Write-Host "[source] Bound FlexFactor runtime to verified GitHub main $upstream; restarting." -ForegroundColor Green
        Restart-FlexFactorLauncher -LauncherPath $LauncherPath -ForwardedArgs $ForwardedArgs
    }
}
