# Put FlexFactor's entry points on the desktop.
#
# README says FlexFactor is "driven from desktop shortcuts or the command line",
# and nothing in the repo ever created those shortcuts - so on a fresh machine
# (and on the owner's own, checked 2026-08-28) the documented way to start it did
# not exist. This script is that missing half, and it is idempotent: run it again
# after moving the checkout and the shortcuts follow.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_desktop_shortcuts.ps1
#
# The desktop is read from the shell folder rather than assumed to be
# %USERPROFILE%\Desktop, because a OneDrive-redirected profile puts it
# somewhere else entirely and a shortcut written to the wrong place is a
# shortcut nobody ever sees.
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop -or -not (Test-Path $desktop)) {
    Write-Error "Could not resolve the desktop folder."
}

$targets = [System.Collections.ArrayList]@(
    @{ Name = 'FlexFactor'
       Script = Join-Path $repo 'flexfactor_launch.ps1'
       Icon = Join-Path $repo 'flexfactor.ico'
       Description = 'FlexFactor - one automatic strongest-paid-to-free model ladder' },
    @{ Name = 'Scout a Program'
       Script = Join-Path $repo 'flexfactor_scout_launch.ps1'
       Icon = Join-Path $repo 'flexfactor_scout.ico'
       Description = 'FlexFactor Scout - find open-source work worth adopting' },
    @{ Name = 'Audit a Program'
       Script = Join-Path $repo 'flexfactor_audit_launch.ps1'
       Icon = Join-Path $repo 'flexfactor.ico'
       Description = 'FlexFactor Audit - line-by-line review that fixes what it finds' }
)

# Windows PowerShell 5.1 is what a .lnk actually launches on this machine, so
# everything here stays inside the 5.1 language surface.
# PER-PROGRAM SHORTCUTS (owner request 2026-08-29). The launcher already accepts
# a dropped path, so one shortcut per program is the same entry point with the
# program pre-filled: click GrantFlow and FlexFactor starts on GrantFlow. They
# carry the SAME flexfactor.ico as the main icon, so they read as FlexFactor
# rather than as separate applications - which is what they are. A path that
# does not exist on this machine is SKIPPED and said out loud, never written as
# a shortcut that fails when clicked.
$programs = @(
    @{ Name = 'GrantFlow';   Path = Join-Path $env:USERPROFILE 'GrantFlow' },
    @{ Name = 'SermonSmith'; Path = Join-Path $env:USERPROFILE 'sermonsmith' },
    @{ Name = 'GeneMap';     Path = Join-Path $env:USERPROFILE 'genemap-discovery' }
)
foreach ($prog in $programs) {
    if (-not (Test-Path $prog.Path)) {
        # SKIPPING IS NOT ENOUGH ON A RE-RUN. This installer is idempotent, so
        # it runs again after a program folder is moved or deleted - and a
        # previous run may have already written "FlexFactor - <name>.lnk" whose
        # embedded argument points at the path that just disappeared. Merely
        # skipping creation leaves that shortcut on the desktop, breaking the
        # guarantee that a missing program never leaves a shortcut that fails
        # when clicked. Remove the stale one before moving on.
        $staleLnk = Join-Path $desktop ('FlexFactor - ' + $prog.Name + '.lnk')
        if (Test-Path $staleLnk) {
            Remove-Item -LiteralPath $staleLnk -Force -ErrorAction SilentlyContinue
            if (Test-Path $staleLnk) {
                Write-Host ("WARN (could not remove stale shortcut): " + $staleLnk) -ForegroundColor Red
            } else {
                Write-Host ("removed stale shortcut: " + $staleLnk) -ForegroundColor Yellow
            }
        }
        Write-Host ("SKIP (no such folder): " + $prog.Name + " -> " + $prog.Path) -ForegroundColor Yellow
        continue
    }
    [void]$targets.Add(@{
        Name = 'FlexFactor - ' + $prog.Name
        Script = Join-Path $repo 'flexfactor_launch.ps1'
        Icon = Join-Path $repo 'flexfactor.ico'
        Description = 'FlexFactor on ' + $prog.Name + ' (' + $prog.Path + ')'
        Program = $prog.Path
    })
}

$ws = New-Object -ComObject WScript.Shell
foreach ($t in $targets) {
    if (-not (Test-Path $t.Script)) {
        Write-Host ("SKIP (script not found): " + $t.Name) -ForegroundColor Yellow
        continue
    }
    $lnk = Join-Path $desktop ($t.Name + '.lnk')
    $s = $ws.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $s.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $t.Script + '"'
    if ($t.ContainsKey('Program')) {
        # The launcher reads $args as dropped programs, so passing the path here
        # is exactly the drag-and-drop path with the drag already done.
        $s.Arguments += ' "' + $t.Program + '"'
    }
    $s.WorkingDirectory = $repo
    if (Test-Path $t.Icon) { $s.IconLocation = $t.Icon }
    $s.Description = $t.Description
    $s.Save()
    Write-Host ("created " + $lnk) -ForegroundColor Green
}

Write-Host ""
Write-Host "Double-click FlexFactor to choose a mode; drag a file or folder onto it" -ForegroundColor Cyan
Write-Host "to run against that program directly." -ForegroundColor Cyan
