# FlexFactor Audit launcher - double-click the icon, or drag a project folder /
# .lnk / file / URL onto it. Goes straight into audit mode: a line-by-line
# review that fixes every defect it finds, committing each verified cycle on
# the audit branch and merging+pushing green results automatically. EVERY RUN
# IS REAL (owner order 2026-08-11): there is no report-only mode. When both
# API keys are present it runs TWO models (primary + cross-check).
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script = "C:\Users\firer\flexfactor\flexfactor.py"

Write-Host ""
Write-Host "  [##]  FlexFactor Audit" -ForegroundColor Cyan
Write-Host "  Line-by-line, tests every function and button, fixes it all." -ForegroundColor DarkGray
Write-Host ""

# Program targets: audit can take UP TO FIVE programs in one run. Accept
# anything for each - a folder, a file, a .lnk shortcut, a URL, or a plain name.
$programs = @()
$dropped = @($args | Where-Object { $_ })
if ($dropped.Count -ge 1) {
    # Multiple dropped paths: use them all (capped at 5), no prompting.
    $programs = @($dropped | Select-Object -First 5 | ForEach-Object { $_.Trim('"') })
    Write-Host "Programs (dropped): $($programs -join ', ')" -ForegroundColor Green
} else {
    # Ask how many, then read each one. Bad input falls back to 1.
    $countRaw = Read-Host "How many programs to audit? (1-5, Enter = 1)"
    $count = 1
    if (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 5) { $count = 1 }
    for ($i = 1; $i -le $count; $i++) {
        $p = (Read-Host "Program $i (folder, file, .lnk, URL, or name)").Trim('"')
        if (-not [string]::IsNullOrWhiteSpace($p)) { $programs += $p }
    }
    # Cap at 5 collected entries.
    $programs = @($programs | Select-Object -First 5)
}
if ($programs.Count -eq 0) {
    Write-Host "No program given." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}

# Key detection. Audit wants BOTH models when it can get them. Figure out what
# keys are available and pick the primary provider accordingly. Anthropic also
# counts a Bearer credential (ANTHROPIC_AUTH_TOKEN, e.g. the free FCC proxy) -
# same rule as flexfactor.py's _provider_key_present (2026-08-11).
$haveAnthropic = (-not [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) -or (-not [string]::IsNullOrEmpty($env:ANTHROPIC_AUTH_TOKEN))
$haveOpenai    = -not [string]::IsNullOrEmpty($env:OPENAI_API_KEY)

# extraArgs collects every optional flag we add below (apply, economy, etc).
$extraArgs = @()

if ($haveAnthropic -and $haveOpenai) {
    # Both keys present: run primary + cross-check. Do NOT pass --single - we
    # WANT both models reviewing the code.
    Write-Host "Both keys detected - audit prefers both models (primary + cross-check)." -ForegroundColor Green
    Write-Host "FlexFactor live-checks each key at start and auto-falls-back if one is dead (e.g. out of credits)." -ForegroundColor DarkGray
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

# Let the user override the primary provider, but default to what we detected.
$provider = Read-Host "Primary provider [openai / anthropic] (Enter = $defaultProvider)"
if ([string]::IsNullOrWhiteSpace($provider)) { $provider = $defaultProvider }
# GUARD (2026-08-11 live failure): never pass a provider whose credential this
# environment does not carry - a keyless '--provider openai' demoted a whole
# 5-program run to local ollama at preflight. The env wins over the answer.
if ($provider -ne "openai" -and $provider -ne "anthropic") {
    Write-Host "Unknown provider '$provider' - using $defaultProvider." -ForegroundColor Yellow
    $provider = $defaultProvider
}
if ($provider -eq "openai" -and -not $haveOpenai) {
    Write-Host "OPENAI_API_KEY is not set in this environment - using anthropic instead." -ForegroundColor Yellow
    $provider = "anthropic"
}
if ($provider -eq "anthropic" -and -not $haveAnthropic -and $haveOpenai) {
    Write-Host "No Anthropic credential in this environment - using openai instead." -ForegroundColor Yellow
    $provider = "openai"
}
$primary = $provider

# Every run is REAL (owner order 2026-08-11, stronger form: "I do not want test
# runs as part of the app's functions. Each run must be for real."). The CLI no
# longer has --report-only/--dry-run for audit/prodready, so the old
# apply-vs-report prompt is gone. Merge+push are CLI defaults, green-build gated.
$extraArgs += "--apply"
$extraArgs += "--yes"
Write-Host "Apply mode: verified fixes are committed each cycle, merged into the current" -ForegroundColor Yellow
Write-Host "branch, and pushed to origin automatically (green-build gated)." -ForegroundColor Yellow

# Economy mode: author fixes/tests with Claude Sonnet 5 (about 40% cheaper than
# Opus, near-Opus code quality; the build gate + cross-model veto still protect
# every fix). Default is YES because credits are the scarce resource here.
$econ = Read-Host "Economy mode (Sonnet 5 author, cheaper credits)? [Y/n] (Enter = yes)"
if ($econ -match '^(n|no)$') {
    Write-Host "Full mode: Opus 4.8 authors every fix." -ForegroundColor DarkGray
} else {
    $extraArgs += "--economy"
    Write-Host "Economy mode: Sonnet 5 authors fixes; review stays on the cheap judge tier." -ForegroundColor DarkGray
}

# Merge+push into the current branch are automatic now (CLI defaults ON,
# owner order 2026-08-11); pass --no-merge/--no-push at the CLI to opt out.

# Dirty tree walk-away (same fix prodready got for "the GrantFlow failure":
# a run used to hard-stop just because that repo had pre-existing uncommitted
# WIP). Audit's CLI default is OFF, but --snapshot-dirty is safe by
# construction (verbatim preserve as a labeled first commit + fail-closed
# restore, not a silent sweep-in like --allow-dirty), so this launcher
# defaults the prompt to yes regardless of program count.
$snapshotDefault = "yes"
$snap = Read-Host "On a dirty working tree, snapshot the pre-existing changes and continue instead of stopping? [Y/n] (Enter = $snapshotDefault)"
if ([string]::IsNullOrWhiteSpace($snap)) { $snap = $snapshotDefault }
if ($snap -match '^(y|yes)$') {
    $extraArgs += "--snapshot-dirty"
    Write-Host "Dirty trees: pre-existing changes are snapshotted as the sandbox branch's first commit and the audit continues." -ForegroundColor DarkGray
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
# audit auto-detects keys; when both are set it cross-checks with both models.
python $script audit @providerArgs @programArgs @extraArgs
Write-Host ""
Read-Host "Done. Press Enter to close"
