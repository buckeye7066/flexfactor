# amy_weekly_scout.ps1 - Amy's weekly repo scout (owner directive 2026-07-25).
#
# Runs FlexFactor Scout against GrantFlow, REPORT-ONLY, with no prompts, so it
# can run unattended from Task Scheduler (weekly, Sunday 02:00). Searches the
# PRODUCTION Repo Rewards service for repos that would improve GrantFlow's
# crawlers / gap coverage / profile matching, judges benefit per candidate,
# and writes a markdown report. NO code is ever changed by this script
# (apply mode stays a deliberate, human, interactive act via the desktop
# "Scout a Program" shortcut).
#
# The report is archived into GrantFlow's docs so Amy's paper trail accumulates:
#   C:\Users\firer\GrantFlow\docs\amy-scout-reports\YYYY-MM-DD-repo-rewards-scout.md
# Logs: C:\Users\firer\flexfactor\logs\amy_weekly_scout_YYYY-MM-DD.log
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$flexDir   = "C:\Users\firer\flexfactor"
$program   = "C:\Users\firer\GrantFlow"
$rrUrl     = "https://web-production-d7db7.up.railway.app"
$logDir    = Join-Path $flexDir "logs"
$reportDir = Join-Path $program "docs\amy-scout-reports"
$stamp     = Get-Date -Format "yyyy-MM-dd"
$logFile   = Join-Path $logDir "amy_weekly_scout_$stamp.log"

New-Item -ItemType Directory -Force $logDir | Out-Null
New-Item -ItemType Directory -Force $reportDir | Out-Null

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    $line | Tee-Object -FilePath $logFile -Append
}

Log "Amy weekly scout starting (report-only, Repo Rewards: $rrUrl)"

# Provider: openai by default (matches the interactive launcher); fall back to
# anthropic if only that key is present. Task Scheduler runs as the user, so
# user-level environment variables are available.
$provider = "openai"
if ([string]::IsNullOrEmpty($env:OPENAI_API_KEY)) {
    if (-not [string]::IsNullOrEmpty($env:ANTHROPIC_API_KEY)) {
        $provider = "anthropic"
        Log "OPENAI_API_KEY not set; falling back to anthropic provider."
    } else {
        Log "ERROR: no OPENAI_API_KEY or ANTHROPIC_API_KEY in environment. Aborting."
        exit 1
    }
}

Set-Location $flexDir
# --report-only: advisory output only. --no-auto-start: use the cloud service,
# never boot the local repo-rewards stack at 2 AM.
python "$flexDir\flexfactor.py" scout `
    --program $program `
    --provider $provider `
    --report-only `
    --repo-rewards-url $rrUrl `
    --no-auto-start 2>&1 | Tee-Object -FilePath $logFile -Append
$scoutExit = $LASTEXITCODE
Log "Scout finished with exit code $scoutExit"

# Archive the report (scout writes <slug>_repo_rewards_report.md into the
# program folder). Move it into GrantFlow's docs, date-stamped.
$report = Get-ChildItem -Path $program -Filter "*_repo_rewards_report.md" -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($report) {
    $dest = Join-Path $reportDir "$stamp-repo-rewards-scout.md"
    Move-Item -Force $report.FullName $dest
    Log "Report archived: $dest"
} else {
    Log "WARNING: no scout report file found in $program"
}

Log "Amy weekly scout done."
exit $scoutExit
