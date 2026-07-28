#!/bin/bash
# Hermes Desktop launcher — single instance, pre-starts backend for fast startup.
# Cleans stale processes + scopes, auto-heals dist/merge corruption, NVIDIA guard.

SENTINEL="/tmp/hermes-desktop-quit.sentinel"
PIDFILE="/tmp/hermes-desktop.pid"
HERMES_HOME="$HOME/.hermes"
HERMES_AGENT="$HERMES_HOME/hermes-agent"
DESKTOP_DIR="$HERMES_AGENT/apps/desktop"
ELECTRON="$HERMES_AGENT/node_modules/.bin/electron"
VENV_PYTHON="$HERMES_AGENT/venv/bin/python"

export HERMES_DESKTOP_DISABLE_GPU=
export ELECTRON_OZONE_PLATFORM_HINT=auto

cd "$DESKTOP_DIR" || exit 1
MAIN_TS="$DESKTOP_DIR/electron/main.ts"

# --- Auto-heal guards ---

# HTML MIME guard
HERMES_DESKTOP_FILE="$HOME/.local/share/applications/Hermes.desktop"
if grep -q 'text/html' "$HERMES_DESKTOP_FILE" 2>/dev/null; then
  sed -i 's/;text\/html//g; s/text\/html;//g; s/^MimeType=;/MimeType=/g' "$HERMES_DESKTOP_FILE"
fi

# Merge-artifact guard — auto-restore *** corruption from git
if grep -q '<<<<<<<\|>>>>>>>\|token: \*\*\*\|wsUrl.*token=\*\*\*\|supportsPassword: \*\*\*' "$MAIN_TS" 2>/dev/null; then
  echo "[hermes-launcher] Merge artifacts in main.ts — restoring..."
  git checkout HEAD -- "$MAIN_TS" 2>/dev/null
fi

# Stale-dist guard — rebuild if source newer
if [ "electron/main.ts" -nt "dist/electron-main.mjs" ] 2>/dev/null; then
  echo "[hermes-launcher] Source newer than dist — rebuilding..."
  npm run build 2>&1 | tail -5
fi

# NVIDIA guard — detect version mismatch
if [ -f /proc/driver/nvidia/version ] && command -v modinfo >/dev/null 2>&1; then
  LOADED=$(sed -n 's/.*Kernel Module  *\([0-9.]*\) .*/\1/p' /proc/driver/nvidia/version 2>/dev/null | head -1)
  ONDISK=$(modinfo -F version nvidia 2>/dev/null | head -1)
  if [ -n "$LOADED" ] && [ -n "$ONDISK" ] && [ "$LOADED" != "$ONDISK" ]; then
    echo "[hermes-launcher] NVIDIA driver mismatch — loaded: $LOADED, on-disk: $ONDISK."
    echo "[hermes-launcher] Reboot required. GPU apps cannot start."
    exit 1
  fi
fi

# Kill stale processes + scopes
kill -9 $(ps aux | grep 'hermes-agent.*electron\|electron.*desktop' | grep -v grep | awk '{print $2}') 2>/dev/null
systemctl --user reset-failed 2>/dev/null
for pid in $(systemctl --user show 'app-org.chromium.Chromium-*.scope' -p MainPID --value 2>/dev/null | grep -v '^0$'); do
  kill "$pid" 2>/dev/null
done
sleep 1
systemctl --user kill app-org.chromium.Chromium-*.scope 2>/dev/null
systemctl --user reset-failed 2>/dev/null
rm -f /tmp/hermes-desktop*.pid /tmp/hermes-desktop-quit.sentinel

# PID lock
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "Already running (PID $(cat "$PIDFILE")). Exiting."
  exit 0
fi
echo $$ > "$PIDFILE"
cleanup() { rm -f "$SENTINEL" "$PIDFILE"; kill "$SERVE_PID" 2>/dev/null; exit 0; }
trap cleanup EXIT INT TERM

# --- Pre-start serve backend (avoids desktop spawning its own ~5s startup) ---
# ponytail: pre-started serve means desktop skips spawn, connects via env vars instantly
TOKEN=$(openssl rand -hex 32)
BACKEND_LOG="$HOME/.hermes/logs/backend.log"
mkdir -p "$HOME/.hermes/logs"
rm -f "$BACKEND_LOG"
"$VENV_PYTHON" -m hermes_cli.main serve --host 127.0.0.1 --port 0 >"$BACKEND_LOG" 2>&1 &
SERVE_PID=$!

PORT=""
for i in $(seq 1 20); do
  if grep -q 'HERMES_BACKEND_READY port=' "$BACKEND_LOG" 2>/dev/null; then
    PORT=$(grep -oP 'port=\K\d+' "$BACKEND_LOG" | tail -1)
    break
  fi
  sleep 0.5
done

if [ -z "$PORT" ]; then
  echo "[hermes-launcher] Backend not ready after 10s — launching without pre-start."
  kill "$SERVE_PID" 2>/dev/null
else
  export HERMES_DESKTOP_REMOTE_URL="http://127.0.0.1:$PORT"
  export HERMES_DESKTOP_REMOTE_TOKEN="${TOKEN}"
  echo "[hermes-launcher] Backend ready on port $PORT — desktop connects directly."
fi

cd "$DESKTOP_DIR" && "$ELECTRON" . --no-sandbox --in-process-gpu --disable-gpu-compositing --disable-dev-shm-usage --no-zygote --disable-features=UseSystemdServiceManager --ozone-platform-hint=auto --enable-features=UseOzonePlatform
