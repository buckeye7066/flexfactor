# FlexFactor Scout shortcut: compare up to 30 entered program URLs against one
# explicit target, strictly sequential.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$script = Join-Path $PSScriptRoot "flexfactor_run.py"
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')

$target = (Read-Host "Target program to optimize (folder, file, shortcut, URL, or description)").Trim('"')
if ([string]::IsNullOrWhiteSpace($target)) {
    Write-Host "The target program cannot be blank." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}

$programs = @($args | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") } |
    ForEach-Object { "$($_)".Trim('"') })
if ($programs.Count -gt 30) {
    Write-Host "Choose no more than 30 programs." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}
if ($programs.Count -eq 0) {
    $countRaw = Read-Host "How many programs to scout? (1-30, Enter = 1)"
    $count = 1
    if (-not [string]::IsNullOrWhiteSpace($countRaw) -and
        (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 30)) {
        Write-Host "Program count must be from 1 through 30." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 2
    }
    for ($index = 1; $index -le $count; $index++) {
        $program = (Read-Host "Public program/product URL to scout $index (repositories belong in Repo Rewards)").Trim('"')
        if ([string]::IsNullOrWhiteSpace($program)) {
            Write-Host "Program $index cannot be blank." -ForegroundColor Red
            Read-Host "Press Enter to close"
            exit 2
        }
        $programs += $program
    }
}

$costRaw = Read-Host "Maximum paid-model cost in USD (1-150, Enter = 150)"
$cost = 150
if (-not [string]::IsNullOrWhiteSpace($costRaw) -and
    (-not [int]::TryParse($costRaw, [ref]$cost) -or $cost -lt 1 -or $cost -gt 150)) {
    Write-Host "Cost must be from 1 through 150." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}

$contextConsent = Read-Host "Scout may send target and scouted-program evidence to the selected hosted model. Type YES to allow it"
if ($contextConsent -cne "YES") {
    Write-Host "Scout cancelled; program source context was not sent." -ForegroundColor Yellow
    Read-Host "Press Enter to close"
    exit 2
}

$cliArgs = @("scout", "--target", $target, "--model-mode", "best", "--max-cost", "$cost",
             "--allow-remote-program-context")
foreach ($program in $programs) { $cliArgs += @("--program", $program) }

Write-Host ""
Write-Host "FlexFactor Scout" -ForegroundColor Cyan
Write-Host "Target: $target" -ForegroundColor DarkGray
Write-Host "$($programs.Count) program(s) to scout, one at a time, controlled by the orchestrator." -ForegroundColor DarkGray
Write-Host "Model policy: strongest paid capacity first, descending through free." -ForegroundColor DarkGray
Write-Host "Scout writes evidence and proposals; it does not claim unpublished code as applied." -ForegroundColor DarkGray
Write-Host ""
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs (@($script) + $cliArgs)
$exitCode = $LASTEXITCODE
Write-Host ""
Read-Host "Done. Press Enter to close"
exit $exitCode
