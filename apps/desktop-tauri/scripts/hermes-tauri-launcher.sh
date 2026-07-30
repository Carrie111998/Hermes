#!/bin/bash
# Hermes Tauri launcher — connects to shared systemd-managed backend.
# Backend lifecycle managed by: systemctl --user hermes-serve.service

HERMES_HOME="$HOME/.hermes"
HERMES_AGENT="$HERMES_HOME/hermes-agent"
TAURI_BINARY="$HERMES_AGENT/apps/desktop-tauri/src-tauri/target/release/hermes-tauri"
PIDFILE="/tmp/hermes-tauri.pid"

log() { echo "[hermes-tauri] $*"; notify-send "Hermes Tauri" "$*" 2>/dev/null || true; }

# --- integrity check - catch *** corruptions in Python backend ---
# Uses py_compile for real syntax errors (zero false positives)
PYTHON_CORRUPT=$(python3 -c "
import py_compile, glob, sys
bad = []
for f in glob.glob('$HERMES_AGENT/hermes_cli/*.py'):
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        bad.append(f.split('/')[-1] + ':' + str(e).split('line ')[-1].split(')')[0])
if bad:
    print('; '.join(bad))
    sys.exit(1)
" 2>&1)
if [ -n "$PYTHON_CORRUPT" ]; then
  log "*** corruption in Python backend -- fix before launch"
  echo "$PYTHON_CORRUPT"
  exit 1
fi

# NVIDIA guard
if [ -f /proc/driver/nvidia/version ] && command -v modinfo >/dev/null 2>&1; then
  LOADED=$(awk '{print $8}' /proc/driver/nvidia/version 2>/dev/null | head -1)
  ONDISK=$(modinfo -F version nvidia 2>/dev/null | head -1)
  if [ -n "$LOADED" ] && [ -n "$ONDISK" ] && [ "$LOADED" != "$ONDISK" ]; then
    log "NVIDIA driver mismatch -- reboot required."
    exit 1
  fi
fi

# PID lock (Tauri process only, backend managed by systemd)
if [ -f "$PIDFILE" ]; then
  LOCK_PID=$(cat "$PIDFILE")
  if kill -0 "$LOCK_PID" 2>/dev/null; then
    log "Already running. Exiting."
    exit 0
  fi
  rm -f "$PIDFILE"
fi
echo $$ > "$PIDFILE"
cleanup() { rm -f "$PIDFILE"; }
trap cleanup EXIT INT TERM

# --- Read shared token from systemd-managed backend ---
log "Using shared backend (systemd hermes-serve.service)"

TOKEN_FILE="$HOME/.hermes/runtime/backend-token"
if [ -f "$TOKEN_FILE" ]; then
  SECRET=$(cat "$TOKEN_FILE")
  export HERMES_DASHBOARD_SESSION_TOKEN="$SECRET"
  log "Connected to shared backend on :44985"
else
  log "ERROR: No backend-token found at $TOKEN_FILE"
  log "Run: systemctl --user start hermes-serve"
  exit 1
fi

# WebKit workaround + profile
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export HERMES_PROFILE="${HERMES_PROFILE:-dimitri}"

log "Launching Tauri..."
"$TAURI_BINARY"
