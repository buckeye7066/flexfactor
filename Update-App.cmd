@echo off
setlocal
cd /d "%~dp0"
python "%~dp0source_app_update.py" --repo buckeye7066/flexfactor --name "flexfactor" --prompt
if errorlevel 10 echo Update installed. Reopen the app through its normal launcher.
