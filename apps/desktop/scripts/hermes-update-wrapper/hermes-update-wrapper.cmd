@echo off
rem ----------------------------------------------------------------------
rem hermes-update-wrapper.cmd
rem
rem Entry point invoked by the desktop's in-app Update flow. The desktop
rem spawns 'hermes-setup.exe' (which now points to this .cmd via
rem resolveUpdaterBinary patch). We hand off to the PowerShell wrapper
rem which waits for the desktop to exit, then runs the real installer.
rem
rem Forwards all args to the PowerShell wrapper. Exits with whatever
rem exit code the wrapper (and ultimately the real installer) returns.
rem
rem -WindowStyle Hidden keeps PowerShell from popping a console. The
rem desktop already passes windowsHide:true when it spawns this .cmd
rem (see apps/desktop/electron/{windows-child-options,updater-process}.ts),
rem so this cmd window should not appear either. All output is captured
rem in the log file at %LOCALAPPDATA%\hermes\logs\update-wrapper.log.
rem ----------------------------------------------------------------------

setlocal
set "SCRIPT_DIR=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" ^
    -NoProfile ^
    -WindowStyle Hidden ^
    -ExecutionPolicy Bypass ^
    -File "%SCRIPT_DIR%hermes-update-wrapper.ps1" ^
    %*
exit /b %ERRORLEVEL%
