@echo off
REM Camofox stealth-browser server — hardened, loopback-only.
cd /d "D:\projects\camofox-server"
set "CAMOFOX_HOST=127.0.0.1"
set "CAMOFOX_PORT=9377"
set "CAMOFOX_AUTH_MODE=required"
set "CAMOFOX_ALLOW_PRIVATE_NETWORK=false"
set "CAMOFOX_HEADLESS=true"
REM Keep camofox alive across full research sessions instead of idle-exiting every ~30 min.
REM CAMOFOX_IDLE_TIMEOUT_MS = ms since last activity before cleanup starts.
REM CAMOFOX_IDLE_EXIT_TIMEOUT_MS = ms after cleanup before the process exits.
REM 86400000 = 24h. The camofox-keepalive cron remains as a crash-recovery net.
set "CAMOFOX_IDLE_TIMEOUT_MS=86400000"
set "CAMOFOX_IDLE_EXIT_TIMEOUT_MS=86400000"
set /p CAMOFOX_API_KEY=<"D:\secrets\camofox-api-key.txt"
set /p CAMOFOX_ADMIN_KEY=<"D:\secrets\camofox-admin-key.txt"
node "node_modules\camofox-browser\dist\src\server.js" >> "D:\projects\camofox-server\camofox.log" 2>&1
