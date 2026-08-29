# The ONE answer to "which interpreter runs FlexFactor".
#
# README's install path is `py -3.12 -m venv .venv` + `pip install -e ".[all]"`,
# so on a fresh machine the provider SDKs exist ONLY inside `.venv`. All three
# launchers called bare `python`, which resolves to whatever is first on PATH -
# a global interpreter that may be the wrong version and almost certainly has no
# `anthropic` / `openai` installed. Double-clicking the documented desktop
# shortcut would then fail on an import, pointing at nothing the owner did wrong.
#
# Dot-source this from every launcher so the answer cannot drift between them:
#
#     . (Join-Path $PSScriptRoot 'scripts\flexfactor_python.ps1')
#     $py = Get-FlexFactorPython -Repo $PSScriptRoot
#     & $py $script @args
#
# Windows PowerShell 5.1 is what a .lnk launches, so this stays inside 5.1.
function Get-FlexFactorPython {
    param([Parameter(Mandatory = $true)][string]$Repo)

    # An explicit choice always wins. This is how a host with several Pythons
    # pins the one FlexFactor should use, and it is what makes the resolution
    # TESTABLE: the launcher-parity suite stubs a `python` command and would
    # otherwise be bypassed by whatever this function discovered on the machine
    # running the tests - a gate that silently stops covering the thing it is
    # named after.
    if ($env:FLEXFACTOR_PYTHON) { return $env:FLEXFACTOR_PYTHON }

    # The checkout's own environment wins next - that is where
    # `pip install -e ".[all]"` put the SDKs.
    $venv = Join-Path $Repo '.venv\Scripts\python.exe'
    if (Test-Path $venv) { return $venv }

    # No .venv: fall back to PATH exactly as before, so an owner who installed
    # globally is not broken by this change. `py -3.12` is preferred over bare
    # `python` when present because the project requires >=3.12 and the Windows
    # launcher can pick the right one.
    # Select-Object -First 1 is load-bearing: `py.exe` exists twice on a stock
    # Windows (C:\WINDOWS and the WindowsApps shim), and `.Path` on the resulting
    # ARRAY yields both paths joined by a space - a command name that does not
    # exist, which then printed a CommandNotFoundException on every launch.
    $py = $null
    try {
        $py = (Get-Command -Name 'py' -CommandType Application -ErrorAction Stop |
               Select-Object -First 1).Path
    } catch {}
    if ($py) {
        # `py -3.12 -c ...` succeeding is the only proof that version exists.
        & $py -3.12 -c "import sys" 2>$null 1>$null
        if ($LASTEXITCODE -eq 0) { return @($py, '-3.12') }
    }
    return 'python'
}

# Invoke FlexFactor with the resolved interpreter. Handles the two shapes
# Get-FlexFactorPython can return (a single exe, or py + version switch) so no
# caller has to.
function Invoke-FlexFactorPython {
    param(
        [Parameter(Mandatory = $true)][string]$Repo,
        [Parameter(Mandatory = $true)][string[]]$PyArgs
    )
    $py = Get-FlexFactorPython -Repo $Repo
    if ($py -is [array]) { & $py[0] $py[1] @PyArgs } else { & $py @PyArgs }
}
