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

# Python corruption guard — detect *** or syntax errors in backend
PYTHON_FILES=$(find "$HERMES_AGENT/hermes_cli" -name '*.py' -not -path '*__pycache__*' 2>/dev/null)
if [ -n "$PYTHON_FILES" ]; then
  BAD=""
  for f in $PYTHON_FILES; do
    if grep -nE '\*\*\*' "$f" 2>/dev/null | grep -v 'is_password=\*\*\*\|token=\*\*\*\|secret: \*\*\*\|client_secret: \*\*\*' | grep -q .; then
      echo "[hermes-launcher] *** corruption in $f — restoring from git"
      git -C "$HERMES_AGENT" checkout HEAD -- "${f#$HERMES_AGENT/}" 2>/dev/null && BAD="$BAD $f"
    fi
    python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null
    if [ $? -ne 0 ]; then
      echo "[hermes-launcher] SyntaxError in $f — restoring from git"
      git -C "$HERMES_AGENT" checkout HEAD -- "${f#$HERMES_AGENT/}" 2>/dev/null
      python3 -c "import py_compile; py_compile.compile('$f', doraise=True)" 2>/dev/null || {
        echo "[hermes-launcher] FATAL: $f still broken after git restore — aborting"
        exit 1
      }
    fi
  done
  [ -n "$BAD" ] && echo "[hermes-launcher] Restored corrupted Python files:$BAD"
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
cleanup() { rm -f "$SENTINEL" "$PIDFILE"; exit 0; }
trap cleanup EXIT INT TERM

# --- Connect to shared systemd-managed backend (hermes-serve.service) ---
# systemd user service manages hermes-serve lifecycle on port 44985.
# Both Electron + Tauri read the same token from ~/.hermes/runtime/backend-token.
TOKEN_FILE="$HOME/.hermes/runtime/backend-token"
if [ -f "$TOKEN_FILE" ]; then
  BACKEND_TOKEN=$(cat "$TOKEN_FILE")
  export HERMES_DESKTOP_REMOTE_URL="http://127.0.0.1:44985"
  export HERMES_DESKTOP_REMOTE_TOKEN="$BACKEND_TOKEN"
  echo "[hermes-launcher] Connected to shared backend on :44985"
else
  echo "[hermes-launcher] WARNING: No backend-token found at $TOKEN_FILE"
  echo "[hermes-launcher] Run: systemctl --user start hermes-serve"
fi

cd "$DESKTOP_DIR" && "$ELECTRON" . --no-sandbox --in-process-gpu --disable-gpu-compositing --disable-dev-shm-usage --no-zygote --disable-features=UseSystemdServiceManager --ozone-platform-hint=auto --enable-features=UseOzonePlatform
