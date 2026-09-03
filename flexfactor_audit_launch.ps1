# FlexFactor Audit shortcut: up to 30 repositories, strictly sequential.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$launcherArguments = @($args)
. (Join-Path $PSScriptRoot 'scripts\flexfactor_source_refresh.ps1')
Invoke-FlexFactorSourceRefresh -Repository $PSScriptRoot `
    -LauncherPath $PSCommandPath -ForwardedArgs $launcherArguments

$script = Join-Path $PSScriptRoot "flexfactor_run.py"
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')

$programs = @($args | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") } |
    ForEach-Object { "$($_)".Trim('"') })
if ($programs.Count -gt 30) {
    Write-Host "Choose no more than 30 repositories." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}
if ($programs.Count -eq 0) {
    $countRaw = Read-Host "How many repositories to audit? (1-30, Enter = 1)"
    $count = 1
    if (-not [string]::IsNullOrWhiteSpace($countRaw) -and
        (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 30)) {
        Write-Host "Repository count must be from 1 through 30." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 2
    }
    for ($index = 1; $index -le $count; $index++) {
        $program = (Read-Host "Repository $index (folder, file, shortcut, URL, or name)").Trim('"')
        if ([string]::IsNullOrWhiteSpace($program)) {
            Write-Host "Repository $index cannot be blank." -ForegroundColor Red
            Read-Host "Press Enter to close"
            exit 2
        }
        $programs += $program
    }
}

$sessionPrompt = Read-Host "Session prompt for the selected repositories (Enter = none)"

$costRaw = Read-Host "Maximum paid-model cost in USD (1-150, Enter = 150)"
$cost = 150
if (-not [string]::IsNullOrWhiteSpace($costRaw) -and
    (-not [int]::TryParse($costRaw, [ref]$cost) -or $cost -lt 1 -or $cost -gt 150)) {
    Write-Host "Cost must be from 1 through 150." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}

$cliArgs = @("audit", "--model-mode", "best", "--max-cost", "$cost",
             "--max-cycles", "6", "--apply", "--yes", "--auto-clean")
foreach ($program in $programs) { $cliArgs += @("--program", $program) }
if (-not [string]::IsNullOrWhiteSpace($sessionPrompt)) {
    $cliArgs += @("--session-prompt", $sessionPrompt)
}

Write-Host ""
Write-Host "FlexFactor Audit" -ForegroundColor Cyan
Write-Host "$($programs.Count) target(s), one at a time, in the selected order." -ForegroundColor DarkGray
Write-Host "The orchestrator starts with the strongest paid capacity and descends to free." -ForegroundColor DarkGray
Write-Host "Pass 1 covers the repository; later passes cover only the preceding verified edit delta." -ForegroundColor DarkGray
Write-Host "Between passes 1 and 2, the top three competitor capabilities are attempted." -ForegroundColor DarkGray
Write-Host "Success requires independent review and the exact commit on origin's default branch." -ForegroundColor DarkGray
Write-Host ""
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs (@($script) + $cliArgs)
$exitCode = $LASTEXITCODE
Write-Host ""
Read-Host "Done. Press Enter to close"
exit $exitCode
