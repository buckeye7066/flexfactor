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

$targets = @(
    @{ Name = 'FlexFactor'
       Script = Join-Path $repo 'flexfactor_launch.ps1'
       Icon = Join-Path $repo 'flexfactor.ico'
       Description = 'FlexFactor - refactor / scout / audit / prodready (free or paid)' },
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
    $s.WorkingDirectory = $repo
    if (Test-Path $t.Icon) { $s.IconLocation = $t.Icon }
    $s.Description = $t.Description
    $s.Save()
    Write-Host ("created " + $lnk) -ForegroundColor Green
}

Write-Host ""
Write-Host "Double-click FlexFactor to choose a mode; drag a file or folder onto it" -ForegroundColor Cyan
Write-Host "to run against that program directly." -ForegroundColor Cyan
