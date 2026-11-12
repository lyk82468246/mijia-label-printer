@echo off
setlocal

rem One-click launcher: starts the backend detached, opens the browser, then
rem closes the current terminal window. Optional arguments are passed to the
rem PowerShell launcher, for example:  start_label_printer.cmd -Port 8072
set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%scripts\start_label_printer.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

exit /b %EXIT_CODE%
