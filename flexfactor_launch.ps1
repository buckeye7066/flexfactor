# FlexFactor launcher - double-click the desktop icon, or drag a source file onto it.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# ---- Route through the local FCC proxy (Free Claude Code) instead of the ----
# ---- real Anthropic/OpenAI APIs. The proxy on 127.0.0.1:8082 exposes only  ----
# ---- the Anthropic Messages API and takes Bearer token 'freecc'. It maps   ----
# ---- any Claude model id by tier (opus/sonnet/haiku) to a free upstream,   ----
# ---- so FlexFactor's claude-* ids route fine. The OpenAI provider is left  ----
# ---- unusable here because the proxy has no OpenAI inbound endpoint.       ----
# To revert: copy flexfactor_launch.ps1.bak-preproxy over this file and your
# real ANTHROPIC_API_KEY / OPENAI_API_KEY will be used again.
$env:ANTHROPIC_BASE_URL  = "http://127.0.0.1:8082"
$env:ANTHROPIC_AUTH_TOKEN = "freecc"      # Bearer auth the proxy expects
$env:ANTHROPIC_API_KEY   = ""             # blank any real key so the SDK uses the Bearer token
$env:OPENAI_API_KEY      = ""             # proxy is Anthropic-only -> single Anthropic provider
$env:OPENAI_BASE_URL     = ""
$proxyUp = $false
$tcp = New-Object System.Net.Sockets.TcpClient
try { $tcp.Connect("127.0.0.1", 8082); $proxyUp = $true } catch { $proxyUp = $false } finally { $tcp.Close() }
if (-not $proxyUp) {
    Write-Host "  NOTE: the local FCC proxy at 127.0.0.1:8082 is not reachable." -ForegroundColor Yellow
    Write-Host "  FlexFactor is configured to route through it (that is what Claude Code uses)." -ForegroundColor Yellow
    Write-Host "  Start the FCC proxy and retry, or restore flexfactor_launch.ps1.bak-preproxy" -ForegroundColor Yellow
    Write-Host "  to use the real Anthropic/OpenAI API keys again." -ForegroundColor Yellow
    Read-Host "Press Enter to close"; exit 1
}
Write-Host "  Routing through local FCC proxy at 127.0.0.1:8082 (Bearer freecc)." -ForegroundColor Green

$script = "C:\Users\firer\flexfactor\flexfactor.py"

Write-Host ""
Write-Host "  ____  _           ____         _             " -ForegroundColor Cyan
Write-Host " |  _ \| | _____  _|  _ \ __ _  | |_ ___  _ __ " -ForegroundColor Cyan
Write-Host " | |_) | |/ _ \ \/ / |_) / _` | | __/ _ \| '__|" -ForegroundColor Cyan
Write-Host " |  __/| |  __/>  <|  _ < (_| | | || (_) | |   " -ForegroundColor Cyan
Write-Host " |_|   |_|\___/_/\_\_| \_\__,_|  \__\___/|_|  FlexFactor" -ForegroundColor Cyan
Write-Host "  It does reps on your code until the grade is swole." -ForegroundColor DarkGray
Write-Host ""

# Two modes:
#   refactor - do reps on ONE source file until it meets a goal (the original).
#   scout    - search Repo Rewards for repos that would benefit a whole program.
# A dropped file/folder skips straight to that target; otherwise we ask.
$dropped = if ($args.Count -ge 1) { $args[0] } else { $null }

Write-Host "What do you want to do?" -ForegroundColor Yellow
Write-Host "  1) refactor  - improve a single source file until it's swole"
Write-Host "  2) scout     - find Repo Rewards repos that would benefit a program"
Write-Host "  3) audit     - aggressively find+fix every defect, test every function & button"
Write-Host "  4) prodready - hand it any program and walk away: detect the toolchains,"
Write-Host "                 install the deps, fix the defects, score it production ready"
$mode = Read-Host "Choose [1/2/3/4] (Enter = 1)"

# prodready asks NOTHING beyond the program. That is the point of the mode: the
# owner should not have to know which of ~40 audit flags make a run trustworthy.
if ($mode -eq "4") {
    $programs = @()
    $droppedAll = @($args | Where-Object { $_ })
    if ($droppedAll.Count -ge 1) {
        $programs = @($droppedAll | Select-Object -First 5 | ForEach-Object { $_.Trim('"') })
        Write-Host "Programs (dropped): $($programs -join ', ')" -ForegroundColor Green
    } else {
        $p = (Read-Host "Program to make production ready (folder, file, .lnk, URL, or name)").Trim('"')
        if (-not [string]::IsNullOrWhiteSpace($p)) { $programs += $p }
    }
    if ($programs.Count -eq 0) {
        Write-Host "No program given." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }
    $programArgs = @()
    foreach ($p in $programs) { $programArgs += '--program'; $programArgs += $p }
    Write-Host ""
    Write-Host "  Running: detect toolchains -> install dependencies -> review + fix" -ForegroundColor DarkGray
    Write-Host "           -> build gate -> tests -> readiness scorecard" -ForegroundColor DarkGray
    Write-Host "  Fixes land on a flexfactor/prodready-* branch; nothing is pushed." -ForegroundColor DarkGray
    Write-Host ""
    python $script prodready @programArgs --provider anthropic --economy
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
    $extraArgs = @()

    if ($haveAnthropic -and $haveOpenai) {
        # Both keys present: run primary + cross-check. Do NOT pass --single.
        Write-Host "Both keys detected - audit will use both models (primary + cross-check)." -ForegroundColor Green
        $defaultProvider = "anthropic"
    } elseif ($haveAnthropic) {
        Write-Host "Only ANTHROPIC_API_KEY detected - using anthropic." -ForegroundColor Yellow
        $defaultProvider = "anthropic"
    } elseif ($haveOpenai) {
        Write-Host "Only OPENAI_API_KEY detected - using openai." -ForegroundColor Yellow
        $defaultProvider = "openai"
    } else {
        Write-Host "Neither ANTHROPIC_API_KEY nor OPENAI_API_KEY is set in this environment." -ForegroundColor Red
        Write-Host "Set at least one valid key and retry." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }

    $provider = Read-Host "Primary provider [openai / anthropic] (Enter = $defaultProvider)"
    if ([string]::IsNullOrWhiteSpace($provider)) { $provider = $defaultProvider }
    $primary = $provider

    # Programs: audit can take UP TO FIVE in one run. Each can be a folder, file,
    # .lnk, URL, or name. Multiple dropped paths are used as-is (capped at 5).
    $programs = @()
    $droppedAll = @($args | Where-Object { $_ })
    if ($droppedAll.Count -ge 1) {
        $programs = @($droppedAll | Select-Object -First 5 | ForEach-Object { $_.Trim('"') })
        Write-Host "Programs (dropped): $($programs -join ', ')" -ForegroundColor Green
    } else {
        $countRaw = Read-Host "How many programs to audit? (1-5, Enter = 1)"
        $count = 1
        if (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 5) { $count = 1 }
        for ($i = 1; $i -le $count; $i++) {
            $p = (Read-Host "Program $i (folder, file, .lnk, URL, or name)").Trim('"')
            if (-not [string]::IsNullOrWhiteSpace($p)) { $programs += $p }
        }
        $programs = @($programs | Select-Object -First 5)
    }
    if ($programs.Count -eq 0) {
        Write-Host "No program given." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }

    # Apply vs report-only. Default is to apply fixes.
    $apply = Read-Host "Apply fixes? [yes/report] (Enter = yes)"
    if ($apply -eq "report") {
        $extraArgs += "--report-only"
        Write-Host "Report mode: findings only, no code changes." -ForegroundColor DarkGray
    } else {
        Write-Host "Apply mode: verified fixes are committed and pushed each cycle." -ForegroundColor DarkGray
    }

    # Optionally merge verified fixes into the current branch.
    $merge = Read-Host "Merge verified fixes into the current branch? [y/N]"
    if ($merge -match '^(y|yes)$') {
        $extraArgs += "--merge"
    }

    # When 2+ programs were given, offer to run them concurrently.
    if ($programs.Count -ge 2) {
        $par = Read-Host "Run them at the same time (parallel)? [y/N]"
        if ($par -match '^(y|yes)$') {
            $extraArgs += "--parallel"
            $extraArgs += "$($programs.Count)"
        }
    }

    # Build a repeatable --program list, one flag per program.
    $programArgs = @()
    foreach ($p in $programs) { $programArgs += '--program'; $programArgs += $p }
    $providerArgs = @('--provider', $primary)

    Write-Host ""
    python $script audit @providerArgs @programArgs @extraArgs
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
    python $script scout --program $program --provider $provider
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
if (-not (Test-Path $file)) {
    Write-Host "File not found: $file" -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}

$goal = Read-Host "What's the goal? (plain English)"

$threshold = Read-Host "Accept threshold 0-100 (Enter = 90)"
if ([string]::IsNullOrWhiteSpace($threshold)) { $threshold = "90" }

Write-Host ""
python $script refactor --file $file --goal $goal --provider $provider --threshold $threshold
Write-Host ""
Read-Host "Done. Press Enter to close"
