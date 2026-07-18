# FlexFactor Scout launcher - double-click the binoculars icon, or drag a
# project folder / .lnk / file onto it. Goes straight into scout mode: search
# Repo Rewards for repos that would IMPROVE the program you point it at. The
# SAFE DEFAULT is report-only. Choose "apply" (and confirm) to have it integrate
# the improvements that clear the bar (verified build + committed LOCALLY on a
# branch, no push).
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script = "C:\Users\firer\flexfactor\flexfactor.py"

Write-Host ""
Write-Host "  ()  ()   FlexFactor Scout" -ForegroundColor Cyan
Write-Host " (  )(  )  Find code that improves your program -- and apply it." -ForegroundColor DarkGray
Write-Host ""

# Program target: dropped onto the icon, or typed in. Accept anything - a
# folder, a file, a .lnk shortcut, a URL, or a plain description.
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

$provider = Read-Host "Provider [openai / anthropic] (Enter = openai)"
if ([string]::IsNullOrWhiteSpace($provider)) { $provider = "openai" }

# Mode: report (SAFE DEFAULT) writes the report only; apply makes the code changes.
$mode = Read-Host "Mode [report / apply] (Enter = report)"
if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "report" }
$applyArgs = @()
if ($mode -eq "apply") {
    Write-Host "Apply mode: integrations that pass the build are committed LOCALLY to a" -ForegroundColor Yellow
    Write-Host "flexfactor/adopt-* branch (no push). You will be asked to confirm." -ForegroundColor Yellow
    $applyArgs = @("--apply")
} else {
    Write-Host "Report mode: writes the report only; no code changes." -ForegroundColor DarkGray
}

# Key sanity check.
if ($provider -eq "openai" -and [string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
    Write-Host "OPENAI_API_KEY is not set in this environment." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}
if ($provider -eq "anthropic" -and [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) {
    Write-Host "ANTHROPIC_API_KEY is not set. Set a valid sk-ant-... key and retry." -ForegroundColor Red
    Read-Host "Press Enter to close"; exit 1
}

Write-Host ""
# scout auto-starts Repo Rewards if it isn't already running.
python $script scout --program $program --provider $provider @applyArgs
Write-Host ""
Read-Host "Done. Press Enter to close"
