@echo off
REM Modified by Nous Man - 2026-08-03 - CRITICAL #57: single no-argument
REM camofox watchdog wrapper. The cron LLM agent runs ONLY this script so it
REM never constructs a Windows path again (the agent's tool layer kept eating
REM backslashes: "D:projectscamofox-serverstart-camofox-hidden.vbs" ->
REM 6 stuck WSH "Can not find script file" popups on 2026-08-03).
REM
REM Healthy = HTTP 200 AND the JSON field "ok":true. Extra fields
REM (browserConnected:false, poolSize:0) are NORMAL for an idle server and
REM must NOT trigger a restart.
REM
REM Exit 0 = healthy (or successfully started). Exit 1 = genuinely down.
setlocal
set HEALTH_URL=http://127.0.0.1:9377/health
set TMP1=%TEMP%\camofox_ensure_h1.json
set TMP2=%TEMP%\camofox_ensure_h2.json

curl -s -m 10 -o "%TMP1%" -w "%%{http_code}" "%HEALTH_URL%" > "%TMP1%.code" 2>nul
if errorlevel 1 goto START
set /p CODE1=<"%TMP1%.code"
if not "%CODE1%"=="200" goto START
findstr /c:"\"ok\":true" "%TMP1%" >nul
if errorlevel 1 goto START
echo OK: camofox healthy (HTTP 200, ok:true) - nothing to do
exit /b 0

:START
echo camofox unhealthy - starting hidden instance via start-camofox-hidden.vbs ...
cscript //Nologo //B "D:\projects\camofox-server\start-camofox-hidden.vbs"
set /a TRIES=0
:WAITLOOP
set /a TRIES+=1
if %TRIES% GTR 6 goto FAIL
REM ~10s between probes without timeout.exe quirks
ping -n 11 127.0.0.1 >nul
curl -s -m 10 -o "%TMP2%" -w "%%{http_code}" "%HEALTH_URL%" > "%TMP2%.code" 2>nul
if errorlevel 1 goto WAITLOOP
set /p CODE2=<"%TMP2%.code"
if not "%CODE2%"=="200" goto WAITLOOP
findstr /c:"\"ok\":true" "%TMP2%" >nul
if errorlevel 1 goto WAITLOOP
echo OK: camofox started and healthy (HTTP 200, ok:true)
exit /b 0

:FAIL
echo FAIL: camofox did not become healthy within ~60s
exit /b 1
