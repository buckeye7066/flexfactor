# FlexFactor launcher - double-click the desktop icon, or drag a source file onto it.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
# Python otherwise inherits the active Windows ANSI code page in some console
# hosts. A worker printing an arrow/non-breaking hyphen then raises
# UnicodeEncodeError and aborts that program lane. Pin both the interpreter and
# its child-facing stream contract before any FlexFactor process is started.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

# ---- Route ALL model calls through the local FCC proxy (Free Claude Code, ----
# ---- the same backend the "Claude Code - FREE (Ollama)" desktop shortcut  ----
# ---- uses). The proxy on 127.0.0.1:8082 exposes the Anthropic Messages    ----
# ---- API with Bearer token 'freecc' and maps any Claude model id by tier  ----
# ---- to a free upstream (free cloud or local Ollama, per fcc-toggle       ----
# ---- routing), so FlexFactor's claude-* ids route fine.                   ----
# ---- The paid keys are BLANKED for the SDKs but CAPTURED first as RESCUE  ----
# ---- fallbacks (owner order 2026-08-10 evening): the free proxy stays     ----
# ---- PRIMARY for every call; the paid keys' only job is to keep a run     ----
# ---- going when the free backend is overwhelmed, stale, or down. Paid     ----
# ---- rescue spend stays inside FlexFactor's --max-cost budget.            ----
# To run on the real paid APIs again: copy flexfactor_launch.ps1.bak-preproxy
# over this file and your real ANTHROPIC_API_KEY/OPENAI_API_KEY will be used.
$env:FLEXFACTOR_FALLBACK_ANTHROPIC_KEY = $env:ANTHROPIC_API_KEY
$env:FLEXFACTOR_FALLBACK_OPENAI_KEY    = $env:OPENAI_API_KEY
$env:ANTHROPIC_BASE_URL  = "http://127.0.0.1:8082"
$env:ANTHROPIC_AUTH_TOKEN = "freecc"      # Bearer auth the proxy expects
$env:ANTHROPIC_API_KEY   = ""             # blank any real key so the SDK uses the Bearer token
$env:OPENAI_API_KEY      = ""             # blank: no billable OpenAI calls on the free path
$env:OPENAI_BASE_URL     = ""

function Test-FccProxyUp {
    try { return ((Invoke-WebRequest "http://127.0.0.1:8082/health" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) } catch { return $false }
}

function Ensure-FccProxy {
    # Make sure the free FCC proxy is serving; start it if not, the same way
    # fcc-toggle.ps1's Start-Server does (hidden, logs under ~/.fcc/logs,
    # messaging off, bound to 127.0.0.1:8082). Returns $true when healthy.
    if (Test-FccProxyUp) { return $true }
    Write-Host "  FCC proxy not running - starting it (same free backend as the desktop shortcut) ..." -ForegroundColor Yellow
    $fccServer = $null
    try { $fccServer = (Get-Command -Name 'fcc-server' -CommandType Application -ErrorAction Stop).Path } catch {}
    if (-not $fccServer) {
        Write-Host "  fcc-server not found on PATH. Run the 'Claude Code - FREE (Ollama)' desktop" -ForegroundColor Red
        Write-Host "  shortcut once (it starts the proxy), then retry FlexFactor." -ForegroundColor Red
        return $false
    }
    $fccHome = Join-Path $HOME '.fcc'
    $fccLogs = Join-Path $fccHome 'logs'
    if (-not (Test-Path $fccLogs)) { New-Item -ItemType Directory -Path $fccLogs -Force | Out-Null }
    $prevMessaging = $env:MESSAGING_PLATFORM; $prevHost = $env:HOST; $prevPort = $env:PORT
    try {
        if ($env:FCC_ENABLE_MESSAGING -ne '1') { $env:MESSAGING_PLATFORM = 'none' }
        $env:HOST = '127.0.0.1'; $env:PORT = '8082'
        Start-Process -FilePath $fccServer -WorkingDirectory $fccHome `
            -RedirectStandardOutput (Join-Path $fccLogs 'server.stdout.log') `
            -RedirectStandardError (Join-Path $fccLogs 'server.stderr.log') `
            -WindowStyle Hidden
    } finally {
        if ($null -eq $prevMessaging) { Remove-Item Env:\MESSAGING_PLATFORM -ErrorAction SilentlyContinue } else { $env:MESSAGING_PLATFORM = $prevMessaging }
        if ($null -eq $prevHost) { Remove-Item Env:\HOST -ErrorAction SilentlyContinue } else { $env:HOST = $prevHost }
        if ($null -eq $prevPort) { Remove-Item Env:\PORT -ErrorAction SilentlyContinue } else { $env:PORT = $prevPort }
    }
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and -not (Test-FccProxyUp)) { Start-Sleep -Milliseconds 500 }
    if (Test-FccProxyUp) { return $true }
    Write-Host "  The FCC proxy did not come up within 90s." -ForegroundColor Red
    Write-Host "  See $HOME\.fcc\logs\server.stderr.log, or run the desktop shortcut" -ForegroundColor Red
    Write-Host "  'Claude Code - FREE (Ollama)' to diagnose, then retry." -ForegroundColor Red
    return $false
}

function Invoke-FlexFactorJob {
    # Supervisor: the free backend drops connections / hangs under load, so a
    # FlexFactor job that dies is RELAUNCHED (up to 5 attempts) after re-ensuring
    # the proxy is up. Safe by design: the audit sandbox branch is recreated with
    # `git checkout -B` on every run, brain.json's clean_files skip makes a rerun
    # converge instead of starting over, and in-process retries mean a restart
    # only happens when the process genuinely died. Exit 2 (argparse usage
    # error) never retries - rerunning a doomed command 5x helps nobody.
    param([string[]]$JobArgs)
    $maxAttempts = 5
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        if ($selectedRuntimeMode -ne 'paid' -and -not (Ensure-FccProxy)) {
            Write-Host "  Proxy unavailable - cannot run this attempt." -ForegroundColor Red
            return 1
        }
        if ($attempt -gt 1) {
            Write-Host ""
            Write-Host "  RESTART $attempt/$maxAttempts - relaunching the job (free backend dropped it) ..." -ForegroundColor Yellow
        }
        python $script @JobArgs
        $code = $LASTEXITCODE
        if ($code -eq 0) { return 0 }
        if ($code -eq 2) {
            Write-Host "  Exit code 2 (usage error / cancelled) - not retrying." -ForegroundColor Red
            return $code
        }
        if ($code -eq 3) {
            # Exit 3 = the run completed but APPLIED NOTHING despite finding
            # defects (2026-08-11). Relaunching that 4 more times just spends the
            # same money again for the same nothing. Surface it and stop.
            Write-Host "  Exit code 3: the run FIXED NOTHING despite finding defects." -ForegroundColor Red
            Write-Host "  Not retrying - a repeat would re-spend the same budget for the" -ForegroundColor Red
            Write-Host "  same result. See the audit report for why nothing could be applied." -ForegroundColor Red
            return $code
        }
        Write-Host "  FlexFactor exited with code $code." -ForegroundColor Yellow
        if ($attempt -lt $maxAttempts) { Start-Sleep -Seconds 15 }
    }
    Write-Host "  Gave up after $maxAttempts attempts - see output above." -ForegroundColor Red
    return 1
}

# flexfactor_run.py is a thin shim onto flexfactor.run_cli (the same entry the
# installed console script uses); directed orchestration is native to the runtime.
$script = Join-Path $PSScriptRoot "flexfactor_run.py"
$selectedRuntimeMode = "local"

Write-Host ""
Write-Host "  ____  _           ____         _             " -ForegroundColor Cyan
Write-Host " |  _ \| | _____  _|  _ \ __ _  | |_ ___  _ __ " -ForegroundColor Cyan
Write-Host " | |_) | |/ _ \ \/ / |_) / _` | | __/ _ \| '__|" -ForegroundColor Cyan
Write-Host " |  __/| |  __/>  <|  _ < (_| | | || (_) | |   " -ForegroundColor Cyan
Write-Host " |_|   |_|\___/_/\_\_| \_\__,_|  \__\___/|_|  FlexFactor" -ForegroundColor Cyan
Write-Host "  It does reps on your code until the grade is swole." -ForegroundColor DarkGray
Write-Host ""

# Four modes. Keep these descriptions matched to what the CLI actually does -
# a prompt that overstates the mode is how a report-only run gets read as work.
#   refactor - rewrite/grade reps on ONE file until it meets a numeric threshold.
#   scout    - Repo Rewards search for ONE program; PROPOSES, never applies here.
#   audit    - review + fix + publish, up to 10 programs.
#   prodready- audit plus dependency bootstrap and a readiness scorecard.
# A dropped file/folder skips straight to that target; otherwise we ask.
$dropped = if ($args.Count -ge 1) { $args[0] } else { $null }

Write-Host "What do you want to do?" -ForegroundColor Yellow
Write-Host "  1) refactor  - ONE source file + a plain-English goal. Rewrites it, grades the"
Write-Host "                 result, repeats until it scores your threshold (default 90) or"
Write-Host "                 5 reps run out. Writes the file in place."
Write-Host "  2) scout     - ONE program. Searches Repo Rewards for repos that would help it,"
Write-Host "                 judges each against that program's purpose, and REPORTS what is"
Write-Host "                 worth adopting. Proposal only - it changes nothing."
Write-Host "  3) audit     - UP TO 10 programs. Reviews every file against the program's"
Write-Host "                 purpose, researches competitors, exercises its functions, and"
Write-Host "                 fixes what it finds - a second model has to agree each fix is"
Write-Host "                 sound. Commits, and pushes only what builds AND passes the"
Write-Host "                 project's own test suite. Every run is real; no review mode."
Write-Host "  4) prodready - UP TO 10 programs, nothing else to answer. Detects the toolchains"
Write-Host "                 and installs the dependencies first, does everything audit does,"
Write-Host "                 then scores the result against a production-readiness rubric"
Write-Host "                 (unproven gates block; they never pass by default). All programs"
Write-Host "                 run at the same time."
$mode = Read-Host "Choose [1/2/3/4] (Enter = 1)"

# Audit/prodready expose the cost/privacy boundary directly. Local is the
# shortcut-compatible default; paid restores captured real credentials and
# clears the loopback route. The CLI receives the same boundary and refuses
# to cross it even if environment variables are later changed.
if ($mode -eq "3" -or $mode -eq "4") {
    $selectedRuntimeMode = Read-Host "Model mode [local / paid / auto] (Enter = local)"
    if ([string]::IsNullOrWhiteSpace($selectedRuntimeMode)) { $selectedRuntimeMode = "local" }
    $selectedRuntimeMode = $selectedRuntimeMode.ToLowerInvariant()
    if ($selectedRuntimeMode -notin @("local", "paid", "auto")) {
        Write-Host "Unknown model mode '$selectedRuntimeMode'." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 2
    }
    if ($selectedRuntimeMode -eq "paid") {
        $env:ANTHROPIC_API_KEY = $env:FLEXFACTOR_FALLBACK_ANTHROPIC_KEY
        $env:OPENAI_API_KEY = $env:FLEXFACTOR_FALLBACK_OPENAI_KEY
        Remove-Item Env:\ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue
        Remove-Item Env:\ANTHROPIC_BASE_URL -ErrorAction SilentlyContinue
        Remove-Item Env:\OPENAI_BASE_URL -ErrorAction SilentlyContinue
        if ([string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY) -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
            Write-Host "Paid mode requested, but no captured paid credential is available." -ForegroundColor Red
            Read-Host "Press Enter to close"; exit 1
        }
        Write-Host "  Paid mode: only credentialed vendor APIs are permitted; no local fallback." -ForegroundColor Yellow
    } elseif ($selectedRuntimeMode -eq "local") {
        $env:FLEXFACTOR_FALLBACK_ANTHROPIC_KEY = ""
        $env:FLEXFACTOR_FALLBACK_OPENAI_KEY = ""
        Write-Host "  Local mode: free loopback/Ollama routes only; paid rescue is disabled." -ForegroundColor Green
    } else {
        Write-Host "  Auto mode: prefer free/local routes, then configured paid credentials." -ForegroundColor Yellow
    }
}

# prodready asks NOTHING beyond the program. That is the point of the mode: the
# owner should not have to know which of ~40 audit flags make a run trustworthy.
if ($mode -eq "4") {
    # Programs: prodready takes UP TO TEN in one run, same as audit (flexfactor.py's
    # run_audit() validates 1..10 - owner order 2026-08-13). Each can be a folder,
    # file, .lnk, URL, or name. Dropped paths are used as-is.
    $programs = @()
    $droppedAll = @($args | Where-Object { $_ })
    if ($droppedAll.Count -ge 1) {
        $programs = @($droppedAll | Select-Object -First 10 | ForEach-Object { $_.Trim('"') })
        Write-Host "Programs (dropped): $($programs -join ', ')" -ForegroundColor Green
    } else {
        $countRaw = Read-Host "How many programs to make production ready? (1-10, Enter = 1)"
        $count = 1
        if (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 10) { $count = 1 }
        for ($i = 1; $i -le $count; $i++) {
            $p = (Read-Host "Program $i (folder, file, .lnk, URL, or name)").Trim('"')
            if (-not [string]::IsNullOrWhiteSpace($p)) { $programs += $p }
        }
        $programs = @($programs | Select-Object -First 10)
    }
    if ($programs.Count -eq 0) {
        Write-Host "No program given." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }
    $programArgs = @()
    foreach ($p in $programs) { $programArgs += '--program'; $programArgs += $p }
    # prodready runs every program at the same time, no question asked (owner
    # order 2026-08-13: "I want all programs to run at the same time"). run_audit()
    # genuinely fans these out onto a ThreadPoolExecutor of that width - not a
    # cosmetic flag - so N programs really do progress concurrently, each with
    # its own free-proxy/FCC-backed review pool.
    if ($programs.Count -ge 2) {
        $programArgs += '--parallel'
        $programArgs += "$($programs.Count)"
    }
    Write-Host ""
    Write-Host "  Running: detect toolchains -> install dependencies -> review + fix" -ForegroundColor DarkGray
    Write-Host "           -> build gate -> tests -> readiness scorecard" -ForegroundColor DarkGray
    Write-Host "  Verified fixes commit straight to your CURRENT branch and push to" -ForegroundColor DarkGray
    Write-Host "  origin (green-build gated). No sandbox branch, no merge step." -ForegroundColor DarkGray
    if ($programs.Count -ge 2) {
        Write-Host "  Running all $($programs.Count) programs concurrently." -ForegroundColor DarkGray
    }
    Write-Host ""
    # NO '--provider' (owner order 2026-08-11). Passing one marks the choice as
    # EXPLICIT, which suppresses FREE-FIRST and sends the whole run to a paid cloud
    # key - measured that day at ~$2.85/hr while a loaded local qwen3-coder idled.
    # Omitting it lets preflight pick the free local model as author and keep a
    # usable cloud key as the cross-check reviewer. The free-proxy env this launcher
    # sets above still applies to whichever cloud provider is chosen.
    # '--yes': prodready implies --apply, and the CLI still asks for apply
    # confirmation; without a TTY (schtask / piped answers) that gate refuses
    # and the whole run silently degrades to report-only (2026-08-11: 6h /
    # $17.75 GrantFlow review, 3464 defects found, 0 fixed). This menu already
    # IS the owner's confirmation - same reasoning as audit mode above.
    $null = Invoke-FlexFactorJob (@('prodready') + $programArgs + @('--model-mode', $selectedRuntimeMode, '--economy', '--yes'))
    Write-Host ""
    Read-Host "Done. Press Enter to close"
    exit 0
}

# Audit has its own provider handling: it auto-detects keys and (when both are
# set) cross-checks with both models. Branch off before the single-provider
# sanity check used by refactor/scout.
if ($mode -eq "3") {
    # Key detection. Audit wants BOTH models when it can get them.
    $haveAnthropic = (-not [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) -or (-not [string]::IsNullOrEmpty($env:ANTHROPIC_AUTH_TOKEN))
    $haveOpenai    = -not [string]::IsNullOrEmpty($env:OPENAI_API_KEY)
    $extraArgs = @('--model-mode', $selectedRuntimeMode)

    if ($haveAnthropic -and $haveOpenai) {
        # Both keys present: run primary + cross-check. Do NOT pass --single.
        Write-Host "Both keys detected - audit will use both models (primary + cross-check)." -ForegroundColor Green
        $defaultProvider = "anthropic"
    } elseif ($haveAnthropic) {
        # In this launcher the real key was blanked above; what's detected is
        # the free-proxy Bearer token. Say so instead of claiming a paid key.
        Write-Host "Anthropic route detected (free-proxy token) - using anthropic." -ForegroundColor Yellow
        $defaultProvider = "anthropic"
    } elseif ($haveOpenai) {
        Write-Host "Only OPENAI_API_KEY detected - using openai." -ForegroundColor Yellow
        $defaultProvider = "openai"
    } else {
        Write-Host "Neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set in this environment." -ForegroundColor Red
        Write-Host "Set at least one valid key and retry." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }

    # Only ask when there is a REAL choice (both providers usable). With one
    # usable provider the guards below force every answer back to the default,
    # so the question was pure noise (owner feedback 2026-08-11 evening). In
    # this free-proxy launcher OPENAI_API_KEY is always blanked above, so the
    # prompt never fires here at all.
    if ($haveAnthropic -and $haveOpenai) {
        $provider = Read-Host "Primary provider [openai / anthropic] (Enter = $defaultProvider)"
        if ([string]::IsNullOrWhiteSpace($provider)) { $provider = $defaultProvider }
    } else {
        $provider = $defaultProvider
    }
    # GUARD (2026-08-11 live failure): this launcher BLANKS OPENAI_API_KEY for the
    # free-proxy env, so '--provider openai' is guaranteed unusable here and used
    # to demote the whole run to local ollama at preflight. Never pass a provider
    # whose credential this environment does not carry - the env wins.
    if ($provider -ne "openai" -and $provider -ne "anthropic") {
        Write-Host "Unknown provider '$provider' - using $defaultProvider." -ForegroundColor Yellow
        $provider = $defaultProvider
    }
    if ($provider -eq "openai" -and -not $haveOpenai) {
        Write-Host "OPENAI_API_KEY is blank in this environment (free-proxy mode) - using anthropic (free proxy) instead." -ForegroundColor Yellow
        $provider = "anthropic"
    }
    if ($provider -eq "anthropic" -and -not $haveAnthropic -and $haveOpenai) {
        Write-Host "No Anthropic credential in this environment - using openai instead." -ForegroundColor Yellow
        $provider = "openai"
    }
    $primary = $provider

    # Programs: audit can take UP TO TEN in one run (flexfactor.py's run_audit()
    # validates 1..10 - owner order 2026-08-13). Each can be a folder, file, .lnk,
    # URL, or name. Multiple dropped paths are used as-is (capped at 10).
    $programs = @()
    $droppedAll = @($args | Where-Object { $_ })
    if ($droppedAll.Count -ge 1) {
        $programs = @($droppedAll | Select-Object -First 10 | ForEach-Object { $_.Trim('"') })
        Write-Host "Programs (dropped): $($programs -join ', ')" -ForegroundColor Green
    } else {
        $countRaw = Read-Host "How many programs to audit? (1-10, Enter = 1)"
        $count = 1
        if (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 10) { $count = 1 }
        for ($i = 1; $i -le $count; $i++) {
            $p = (Read-Host "Program $i (folder, file, .lnk, URL, or name)").Trim('"')
            if (-not [string]::IsNullOrWhiteSpace($p)) { $programs += $p }
        }
        $programs = @($programs | Select-Object -First 10)
    }
    if ($programs.Count -eq 0) {
        Write-Host "No program given." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }

    # Every run is REAL (owner order 2026-08-11, stronger form: "I do not want
    # test runs as part of the app's functions. Each run must be for real.").
    # The CLI has no --report-only/--dry-run in ANY mode as of 2026-08-21 (the
    # last survivor, scout's --dry-run, was removed outright that day), so this
    # launcher no longer offers a report choice either. --apply --yes keeps the
    # invocation unambiguous for the non-TTY confirmation path.
    # LAUNCHER-DRIFT RULE: both .ps1 launchers must be swept in the SAME commit
    # as any CLI flag change - a stale flag is argparse exit 2 and a dead run.
    if ($true) {
        $extraArgs += "--apply"
        $extraArgs += "--yes"
        Write-Host "Apply mode: verified fixes are committed each cycle, merged into the" -ForegroundColor DarkGray
        Write-Host "current branch, and pushed to origin/main automatically (green-build" -ForegroundColor DarkGray
        Write-Host "gated; owner order 2026-08-11). CLI --no-push/--no-merge opt out." -ForegroundColor DarkGray
    }

    # Repo cleanup is AUTOMATIC and unconditional (owner order 2026-08-20).
    # There is no question here any more. The old prompt asked whether to continue
    # on a dirty working tree; the owner read it as "take care of whatever is left
    # red in the repo first", answered yes, and the run still refused sermonsmith.
    # So the intent is now the implementation: every run cleans the repo BEFORE it
    # starts new work - pre-existing uncommitted changes, open PRs, Dependabot
    # alerts and open issues - then does the new work, then pushes and MERGES to
    # main. Nothing is left dangling and nothing is silently skipped.
    $extraArgs += "--allow-dirty"
    $extraArgs += "--auto-clean"
    Write-Host "Repo cleanup: automatic. Pre-existing changes, open PRs, Dependabot alerts and open issues are cleared FIRST, then the new work runs, then it is pushed and merged to main." -ForegroundColor DarkGray

    # 2+ programs always run at the same time now, no question asked (owner
    # order 2026-08-13: "I want all programs to run at the same time"). Real
    # concurrency - run_audit() fans these out onto a ThreadPoolExecutor of
    # this width, not a cosmetic flag.
    if ($programs.Count -ge 2) {
        $extraArgs += "--parallel"
        $extraArgs += "$($programs.Count)"
        Write-Host "Running all $($programs.Count) programs concurrently." -ForegroundColor DarkGray
    }

    # Build a repeatable --program list, one flag per program.
    $programArgs = @()
    foreach ($p in $programs) { $programArgs += '--program'; $programArgs += $p }
    # Preserve FREE-FIRST for local/auto runs, but paid mode is an explicit
    # vendor choice. Forward the selected PRIMARY. When both paid credentials
    # are present, keep the CLI's default dual-provider contract: the selected
    # provider authors and the other independently cross-checks. The old code
    # printed "both models" above, then appended --single here and silently
    # disabled the promised cross-check.
    if ($selectedRuntimeMode -eq "paid") {
        $extraArgs += "--provider"
        $extraArgs += $primary
        if ($haveAnthropic -and $haveOpenai) {
            $secondary = if ($primary -eq "anthropic") { "openai" } else { "anthropic" }
            Write-Host "Paid audit: $primary is primary; $secondary will independently cross-check." -ForegroundColor Yellow
        } else {
            $extraArgs += "--single"
            Write-Host "Paid audit: only $primary is credentialed; running single-provider." -ForegroundColor Yellow
        }
    }

    Write-Host ""
    $null = Invoke-FlexFactorJob (@('audit') + $programArgs + $extraArgs)
    Write-Host ""
    Read-Host "Done. Press Enter to close"
    exit 0
}

$provider = Read-Host "Provider [anthropic / openai] (Enter = anthropic)"
if ([string]::IsNullOrWhiteSpace($provider)) { $provider = "anthropic" }

# Key sanity check (shared by refactor and scout modes).
if ($provider -eq "openai" -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
    Write-Host "OPENAI_API_KEY is not set in this environment." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
if ($provider -eq "anthropic" -and [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY) -and [string]::IsNullOrEmpty($env:ANTHROPIC_AUTH_TOKEN)) {
    Write-Host "No Anthropic credential set (ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN) and proxy env is missing." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}

if ($mode -eq "2") {
    # Scout mode: the "program" can be a folder, file, .lnk, URL, or description.
    if ($dropped) {
        $program = $dropped
        Write-Host "Program (dropped): $program" -ForegroundColor Green
    } else {
        $program = (Read-Host "Program to help (folder, .lnk, URL, or description)").Trim('"')
    }
    Write-Host ""
    $null = Invoke-FlexFactorJob @('scout', '--program', $program, '--provider', $provider)
    Write-Host ""
    Read-Host "Done. Press Enter to close"
    exit 0
}

# Refactor mode (original behavior).
if ($dropped -and (Test-Path $dropped)) {
    $file = $dropped
    Write-Host "Target file (dropped): $file" -ForegroundColor Green
} else {
    $file = (Read-Host "Path to the source file to improve").Trim('"')
}
# NO local Test-Path bail-out (owner order 2026-08-20). A repo-relative path
# like "backend/crawler-os/contract.js" is the spelling a person actually
# knows, and it is not relative to whatever directory this launcher started
# in. flexfactor.py resolves it against the local checkouts and then the
# owner's GitHub repos, so refusing here would kill the lookup before it runs.
if (-not (Test-Path $file)) {
    Write-Host "Not in this folder - checking your local projects and GitHub repos..." -ForegroundColor DarkGray
}

$goal = Read-Host "What's the goal? (plain English)"

$threshold = Read-Host "Accept threshold 0-100 (Enter = 90)"
if ([string]::IsNullOrWhiteSpace($threshold)) { $threshold = "90" }

Write-Host ""
$null = Invoke-FlexFactorJob @('refactor', '--file', $file, '--goal', $goal, '--provider', $provider, '--threshold', $threshold)
Write-Host ""
Read-Host "Done. Press Enter to close"
