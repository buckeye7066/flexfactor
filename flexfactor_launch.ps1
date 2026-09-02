# FlexFactor desktop launcher. All modes share one production contract:
# up to 30 ordered targets, exactly one active target, at most six passes, and
# the strongest available paid model first before descending to free capacity.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$script = Join-Path $PSScriptRoot "flexfactor_run.py"
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')

function Read-FlexFactorTargets {
    param(
        [string]$Label,
        [object[]]$Dropped
    )

    $targets = @($Dropped | Where-Object { -not [string]::IsNullOrWhiteSpace("$_") })
    if ($targets.Count -gt 30) {
        throw "Choose no more than 30 $Label targets."
    }
    if ($targets.Count -gt 0) {
        return @($targets | ForEach-Object { "$($_)".Trim('"') })
    }

    $countRaw = Read-Host "How many $Label targets? (1-30, Enter = 1)"
    $count = 1
    if (-not [string]::IsNullOrWhiteSpace($countRaw)) {
        if (-not [int]::TryParse($countRaw, [ref]$count) -or $count -lt 1 -or $count -gt 30) {
            throw "Target count must be from 1 through 30."
        }
    }
    for ($index = 1; $index -le $count; $index++) {
        $value = (Read-Host "$Label target $index").Trim('"')
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Target $index cannot be blank."
        }
        $targets += $value
    }
    return $targets
}

function Read-BoundedInteger {
    param(
        [string]$Prompt,
        [int]$Default,
        [int]$Minimum,
        [int]$Maximum
    )
    $raw = Read-Host $Prompt
    if ([string]::IsNullOrWhiteSpace($raw)) { return $Default }
    $value = 0
    if (-not [int]::TryParse($raw, [ref]$value) -or $value -lt $Minimum -or $value -gt $Maximum) {
        throw "Value must be from $Minimum through $Maximum."
    }
    return $value
}

Write-Host ""
Write-Host "FlexFactor" -ForegroundColor Cyan
Write-Host "One orchestrator. Up to 30 targets. One at a time. Six passes maximum." -ForegroundColor DarkGray
Write-Host "Model policy: strongest paid capacity first, then lower paid tiers, then free." -ForegroundColor DarkGray
Write-Host ""
Write-Host "  1) Refactor files"
Write-Host "  2) Scout repository improvements"
Write-Host "  3) Audit and repair repositories"
Write-Host "  4) Make repositories production ready"
$mode = Read-Host "Choose [1/2/3/4] (Enter = 1)"
if ([string]::IsNullOrWhiteSpace($mode)) { $mode = "1" }
if ($mode -notin @("1", "2", "3", "4")) {
    Write-Host "Choose 1, 2, 3, or 4." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}

try {
    $cost = Read-BoundedInteger "Maximum paid-model cost in USD (1-150, Enter = 150)" 150 1 150
    if ($mode -eq "1") {
        $targets = @(Read-FlexFactorTargets "file" @($args))
        $goal = Read-Host "Goal to apply to each selected file"
        if ([string]::IsNullOrWhiteSpace($goal)) { throw "A refactor goal is required." }
        $threshold = Read-BoundedInteger "Acceptance threshold (0-100, Enter = 90)" 90 0 100
        $cliArgs = @("refactor", "--goal", $goal, "--threshold", "$threshold",
                     "--max-iterations", "6", "--max-cost", "$cost",
                     "--model-mode", "best")
        foreach ($target in $targets) { $cliArgs += @("--file", $target) }
    } elseif ($mode -eq "2") {
        $targets = @(Read-FlexFactorTargets "program" @($args))
        $cliArgs = @("scout", "--max-cost", "$cost", "--model-mode", "best",
                     "--allow-remote-program-context")
        foreach ($target in $targets) { $cliArgs += @("--program", $target) }
    } else {
        $label = if ($mode -eq "3") { "audit program" } else { "production program" }
        $targets = @(Read-FlexFactorTargets $label @($args))
        $command = if ($mode -eq "3") { "audit" } else { "prodready" }
        $cliArgs = @($command, "--model-mode", "best", "--max-cost", "$cost",
                     "--max-cycles", "6", "--apply", "--yes", "--no-auto-clean")
        foreach ($target in $targets) { $cliArgs += @("--program", $target) }
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 2
}

Write-Host ""
Write-Host "The orchestrator will run $($targets.Count) target(s) sequentially." -ForegroundColor Cyan
Write-Host "Writing modes succeed only after independent review, project verification," -ForegroundColor DarkGray
Write-Host "and proof that the exact commit is present on origin's default branch." -ForegroundColor DarkGray
Write-Host ""
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs (@($script) + $cliArgs)
$exitCode = $LASTEXITCODE
Write-Host ""
Read-Host "Done. Press Enter to close"
exit $exitCode
