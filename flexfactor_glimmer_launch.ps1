# FlexFactor, running on Muse Glimmer alone.
#
# This is the "run it by itself" entry point (owner decision 2026-08-22):
# Glimmer is deliberately NOT a member of the rotation pool, because rotation is
# cheapest-first and a local model is cost class 0, so a slow local route gets
# picked FIRST every sweep. See _rotation_excluded_reason in flexfactor.py.
#
# Passing --provider ollama --model bypasses the rotator entirely: flexfactor
# only builds a Rotator when _free_first_applies, which requires both that the
# provider is not ollama and that no explicit --model was given. So this script
# needs no new CLI flag -- which also means it cannot trigger the launcher-drift
# trap where a flag added here but missing there kills a run with argparse exit 2.
#
# Usage:
#   .\flexfactor_glimmer_launch.ps1 audit --program myapp
#   .\flexfactor_glimmer_launch.ps1 prodready --program myapp --yes
#   .\flexfactor_glimmer_launch.ps1            # prints what it would run

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest
)

# Not "Stop": on Windows PowerShell 5.1 a native command writing to stderr under
# a blanket Stop preference aborts the whole script, which is how launchers on
# this machine have died silently before.
$ErrorActionPreference = 'Continue'

# Pin UTF-8 for the interpreter AND its child-facing streams. Without this,
# Python inherits the console ANSI code page and a worker printing a non-ASCII
# character raises UnicodeEncodeError mid-run. Same pin as flexfactor_launch.ps1.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$MODEL   = if ($env:GLIMMER_MODEL) { $env:GLIMMER_MODEL } else { 'muse-glimmer:30b' }
$OLLAMA  = 'G:\Programs\AppData\Ollama\ollama.exe'   # the upgraded, registry-tracked install
$BASE    = 'http://127.0.0.1:11434'

# A 30B dense decoder on this CPU generates at roughly 1-1.5 tokens/second, so
# the stock 600s per-call HTTP timeout would kill healthy calls. Raise it well
# past any realistic single generation; the run is bounded by the human, not by
# a timeout that fires on success.
if (-not $env:FLEXFACTOR_OLLAMA_TIMEOUT)     { $env:FLEXFACTOR_OLLAMA_TIMEOUT = '7200' }
# One in-flight call: ollama serves concurrent requests near-serially, and a
# queued second call spends its whole life burning the FIRST call's timeout.
if (-not $env:FLEXFACTOR_OLLAMA_CONCURRENCY) { $env:FLEXFACTOR_OLLAMA_CONCURRENCY = '1' }
$env:OLLAMA_BASE_URL = $BASE

function Test-OllamaUp {
    try {
        $r = Invoke-WebRequest "$BASE/api/version" -TimeoutSec 4 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

if (-not (Test-OllamaUp)) {
    Write-Host '  Ollama is not serving - starting it ...' -ForegroundColor Yellow
    if (-not (Test-Path $OLLAMA)) {
        Write-Error "Ollama not found at $OLLAMA"
        exit 2
    }
    Start-Process -FilePath $OLLAMA -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-OllamaUp) { break }
    }
}
if (-not (Test-OllamaUp)) {
    Write-Error "Ollama did not come up at $BASE"
    exit 2
}

# Refuse to start a long run against a model that is not installed. Failing here
# costs a second; failing at the first model call costs however long it took to
# reach it, and reads like a FlexFactor bug rather than a missing download.
$tags = ''
try { $tags = (Invoke-WebRequest "$BASE/api/tags" -TimeoutSec 10 -UseBasicParsing).Content } catch {}
if ($tags -notmatch [regex]::Escape($MODEL)) {
    Write-Error @"
Model '$MODEL' is not installed in Ollama.

  Official build : ollama pull muse-glimmer:30b
  Your own quant : ollama create muse-glimmer:30b-q4kxl -f C:\Users\firer\glimmer\modelfiles\Modelfile.q4kxl

Then re-run this launcher.
"@
    exit 3
}

$script = Join-Path $PSScriptRoot 'flexfactor_run.py'
if (-not (Test-Path $script)) { Write-Error "missing $script"; exit 2 }

if (-not $Rest -or $Rest.Count -eq 0) {
    Write-Host ''
    Write-Host 'FlexFactor on Glimmer (standalone, no rotation)' -ForegroundColor Cyan
    Write-Host "  model    : $MODEL"
    Write-Host "  timeout  : $($env:FLEXFACTOR_OLLAMA_TIMEOUT)s per call"
    Write-Host "  in-flight: $($env:FLEXFACTOR_OLLAMA_CONCURRENCY)"
    Write-Host ''
    Write-Host '  Give it a mode and a program, e.g.:' -ForegroundColor Yellow
    Write-Host '    .\flexfactor_glimmer_launch.ps1 audit --program myapp'
    Write-Host ''
    Write-Host '  Expect roughly 1-1.5 tokens/second: this box has no GPU Ollama can use.' -ForegroundColor DarkYellow
    exit 0
}

Write-Host "FlexFactor -> $MODEL (standalone, rotation bypassed)" -ForegroundColor Cyan
# RESOLVE THE INTERPRETER THE ONE WAY (launcher-drift fix 2026-08-30). This was
# the last launcher still calling bare `python`, which is the exact defect
# scripts\flexfactor_python.ps1 was written to end: bare `python` takes whatever
# is first on PATH, ignoring $env:FLEXFACTOR_PYTHON and the checkout's .venv -
# and .venv is where `pip install -e ".[all]"` puts the provider SDKs. Measured
# on this host it resolved to C:\Python314\python.exe while the other three
# launchers resolved to the .venv interpreter, so this shortcut ran a DIFFERENT
# Python from every other entry point.
. (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')
Invoke-FlexFactorPython -Repo $PSScriptRoot -PyArgs (@($script) + $Rest + @('--provider', 'ollama', '--model', $MODEL))
exit $LASTEXITCODE
