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
rem ----------------------------------------------------------------------

setlocal
set "SCRIPT_DIR=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" ^
    -NoProfile ^
    -ExecutionPolicy Bypass ^
    -File "%SCRIPT_DIR%hermes-update-wrapper.ps1" ^
    %*
exit /b %ERRORLEVEL%
