# FlexFactor Scout launcher - double-click the binoculars icon, or drag a
# project folder / .lnk / file onto it. Goes straight into scout mode: search
# Repo Rewards for repos that would IMPROVE the program you point it at. The
# SAFE DEFAULT is report-only. Choose "apply" (and confirm) to emit integration
# proposals; target mutation still requires .flexfactor-apply-approval.json.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script = "C:\Users\firer\flexfactor\flexfactor.py"
$productionRr = "https://web-production-d7db7.up.railway.app"

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

# Repo Rewards URL: explicit env wins; else prefer local :3000 when up, else production.
$rrUrl = $env:FLEXFACTOR_REPO_REWARDS_URL
if ([string]::IsNullOrWhiteSpace($rrUrl)) {
    try {
        $null = Invoke-WebRequest -Uri "http://localhost:3000/api/health" -UseBasicParsing -TimeoutSec 2
        $rrUrl = "http://localhost:3000"
        Write-Host "Repo Rewards: local http://localhost:3000" -ForegroundColor DarkGray
    } catch {
        $rrUrl = $productionRr
        Write-Host "Repo Rewards: production $productionRr" -ForegroundColor DarkGray
    }
}

Write-Host ""
$pyArgs = @(
    $script, "scout",
    "--program", $program,
    "--provider", $provider,
    "--repo-rewards-url", $rrUrl,
    "--no-auto-start"
) + $applyArgs
python @pyArgs
Write-Host ""
Read-Host "Done. Press Enter to close"
