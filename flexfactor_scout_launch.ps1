# FlexFactor Scout launcher - double-click the binoculars icon, or drag a
# project folder / .lnk / file onto it. Goes straight into scout mode: search
# Repo Rewards for repos that would IMPROVE the program you point it at. The
# SAFE DEFAULT is report-only. Choose "apply" (and confirm) to emit integration
# proposals; target mutation still requires .flexfactor-apply-approval.json.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Directed entry: same orchestration rule as Factory Deck / Purpose Foundry.
$script = Join-Path $PSScriptRoot "flexfactor_run.py"
# ONE interpreter answer for all three launchers (see the file's header).
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')
$productionRr = if (-not [string]::IsNullOrWhiteSpace($env:FLEXFACTOR_REPO_REWARDS_PRODUCTION_URL)) {
    $env:FLEXFACTOR_REPO_REWARDS_PRODUCTION_URL.TrimEnd("/")
} else {
    "https://web-production-d7db7.up.railway.app"
}
$allowRemote = ($env:FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS -match '^(1|true|yes|on)$')

Write-Host ""
Write-Host "  ()  ()   FlexFactor Scout" -ForegroundColor Cyan
Write-Host " (  )(  )  Find code that improves your program -- and apply it." -ForegroundColor DarkGray
Write-Host ""

if ($args.Count -ge 1 -and $args[0]) {
    $program = $args[0]
    Write-Host "Program (dropped): $program" -ForegroundColor Green
} else {
    $program = (Read-Host "Program to scout (folder, .lnk, URL, or description)").Trim('"')
}
if ([string]::IsNullOrWhiteSpace($program)) {
    Write-Host "No program given." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}

$provider = Read-Host "Provider [openai / anthropic / ollama] (Enter = ollama)"
if ([string]::IsNullOrWhiteSpace($provider)) { $provider = "ollama" }

$contextArgs = @()
if ($provider -ne "ollama") {
    Write-Host ""
    Write-Host "Cloud profiling sends the selected program's source, README, and file tree" -ForegroundColor Yellow
    Write-Host "to $provider. This is separate from Repo Rewards query sharing." -ForegroundColor Yellow
    $shareContext = (Read-Host "Share this program context with $provider? Type YES to continue").Trim()
    if ($shareContext -cne "YES") {
        Write-Host "Cloud program-context sharing was not approved. Nothing sent." -ForegroundColor Red
        Read-Host "Press Enter to close"; exit 1
    }
    $contextArgs = @("--allow-remote-program-context")
}

$mode = Read-Host "Mode [report / apply] (Enter = report)"
if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "report" }
$applyArgs = @()
if ($mode -eq "apply") {
    Write-Host "Apply mode: writes integration PROPOSALS (dependency delta," -ForegroundColor Yellow
    Write-Host "conflict analysis, rollback). Target mutation requires a" -ForegroundColor Yellow
    Write-Host "separate FlexFactor apply approval (.flexfactor-apply-approval.json)." -ForegroundColor Yellow
    $applyArgs = @("--apply")
} else {
    Write-Host "Report mode: writes the report only; no code changes." -ForegroundColor DarkGray
}

if ($provider -eq "openai" -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
    Write-Host "OPENAI_API_KEY is not set in this environment." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
if ($provider -eq "anthropic" -and [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) {
    Write-Host "ANTHROPIC_API_KEY is not set." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
if ($provider -eq "ollama") {
    $ollamaOk = $false
    try {
        $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
        $names = @()
        if ($null -ne $tags.models) {
            $names = @($tags.models | ForEach-Object { $_.name })
        }
        $hasModel = $names | Where-Object {
            $_ -eq "llama3.2:latest" -or $_ -like "llama3.2:*" -or $_ -eq "llama3.2"
        }
        if (-not $hasModel) {
            Write-Host "Ollama is up but default model llama3.2 is not installed." -ForegroundColor Red
            Write-Host "Run: ollama pull llama3.2" -ForegroundColor DarkGray
            Write-Host "Or pass a different provider / install the model you will use." -ForegroundColor DarkGray
            Read-Host "Press Enter to close"; exit 1
        }
        $ollamaOk = $true
    } catch {
        Write-Host "Ollama is not reachable at http://127.0.0.1:11434." -ForegroundColor Red
        Write-Host "Start Ollama, or choose openai/anthropic instead." -ForegroundColor DarkGray
        Read-Host "Press Enter to close"; exit 1
    }
    if (-not $ollamaOk) {
        Read-Host "Press Enter to close"; exit 1
    }
}

function Test-RepoRewardsLocal {
    # Contract probe: /api/version (not /api/health - not required by RR).
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:3000/api/version" -UseBasicParsing -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

# Repo Rewards URL: explicit env wins; else local when up; remote only with opt-in.
$rrUrl = $env:FLEXFACTOR_REPO_REWARDS_URL
$remoteArgs = @()
if ([string]::IsNullOrWhiteSpace($rrUrl)) {
    if (Test-RepoRewardsLocal) {
        $rrUrl = "http://localhost:3000"
        Write-Host "Repo Rewards: local http://localhost:3000" -ForegroundColor DarkGray
    } else {
        # PRODUCTION IS THE DEFAULT FALLBACK (launcher-drift fix 2026-08-30).
        # This launcher still enforced the pre-2026-08-16 opt-in policy and
        # EXITED 1 before the CLI ever ran when local Repo Rewards was down and
        # FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS was unset. The CLI's
        # resolve_repo_rewards_url() has since made production the automatic
        # fallback (opt OUT with --no-remote-repo-rewards or
        # FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=0). Local RR is usually down on
        # this machine, so the desktop shortcut was refusing runs the CLI would
        # have completed, and telling the owner to set an env var the CLI no
        # longer requires. Leave the decision to the CLI, which prints the
        # endpoint it chose. --allow-remote-repo-rewards is still passed when
        # explicitly opted in, as the accepted NO-OP it now is.
        $rrUrl = $productionRr
        if ($allowRemote) { $remoteArgs = @("--allow-remote-repo-rewards") }
        Write-Host "Repo Rewards: local is down; using $productionRr" -ForegroundColor DarkGray
        Write-Host "(opt out with FLEXFACTOR_ALLOW_REMOTE_REPO_REWARDS=0)" -ForegroundColor DarkGray
    }
}

Write-Host ""
$pyArgs = @(
    $script, "scout",
    "--program", $program,
    "--provider", $provider,
    "--repo-rewards-url", $rrUrl,
    "--no-auto-start"
) + $remoteArgs + $contextArgs + $applyArgs
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs $pyArgs
Write-Host ""
Read-Host "Done. Press Enter to close"
