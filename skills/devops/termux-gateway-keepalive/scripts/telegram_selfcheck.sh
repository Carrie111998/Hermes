#!/usr/bin/env bash
# telegram_selfcheck.sh — Verify the gateway's Telegram delivery health on Termux.
# Checks:
#   1. gateway process alive
#   2. gateway_state.json exists
#   3. platforms.telegram.state == 'connected'
#   4. state file freshness (updated within 15 min) — catches zombie connections.
# On failure: logs alert, fires ambient notification, and triggers watchdog restart.
set -u

HOME_DIR="${HOME:-.}"
STATE="$HOME_DIR/.hermes/gateway_state.json"
LOG="$HOME_DIR/.hermes/logs/telegram_selfcheck.log"
NOW=$(date +%s)
ok=1; reasons=""

mkdir -p "$HOME_DIR/.hermes/logs"

# 1) Process alive?
if ! pgrep -f "hermes gateway" >/dev/null 2>&1; then
  ok=0; reasons="$reasons [gateway process DEAD]"
fi

# 2) State file exists?
if [ ! -f "$STATE" ]; then
  ok=0; reasons="$reasons [gateway_state.json missing]"
else
  # 3) Telegram state connected?
  tg=$(python3 -c "
import json, sys
try:
    d = json.load(open('$STATE'))
    print(d.get('platforms', {}).get('telegram', {}).get('state', 'unknown'))
except Exception:
    print('unreadable')
" 2>/dev/null)
  if [ "$tg" != "connected" ]; then
    ok=0; reasons="$reasons [telegram state=$tg]"
  fi

  # 4) State file FRESH? (mtime within 900s)
  mt=$(python3 -c "import os; print(int(os.path.getmtime('$STATE')))" 2>/dev/null || echo 0)
  age=$(( NOW - mt ))
  if [ "$age" -gt 900 ]; then
    ok=0; reasons="$reasons [state STALE: ${age}s old]"
  fi
fi

ts=$(date '+%Y-%m-%d %H:%M:%S')
if [ "$ok" -eq 1 ]; then
  echo "$ts selfcheck: OK" >> "$LOG"
  exit 0
else
  echo "$ts selfcheck: FAIL -$reasons" >> "$LOG"

  # Ambient alert (works offline without network)
  NOTIFY_SCRIPT="$HOME_DIR/.hermes/scripts/presence_notify.sh"
  if [ ! -f "$NOTIFY_SCRIPT" ]; then
    NOTIFY_SCRIPT="$(dirname "$0")/presence_notify.sh"
  fi
  bash "$NOTIFY_SCRIPT" "⚠️ Telegram gateway check FAILED:$reasons. Hermes is alive locally; watchdog recovering." 2>/dev/null || true

  # Trigger watchdog restart if gateway is dead
  if ! pgrep -f "hermes gateway" >/dev/null 2>&1; then
    WATCHDOG_SCRIPT="$HOME_DIR/.hermes/scripts/gateway_watchdog.sh"
    if [ ! -f "$WATCHDOG_SCRIPT" ]; then
      WATCHDOG_SCRIPT="$(dirname "$0")/gateway_watchdog.sh"
    fi
    bash "$WATCHDOG_SCRIPT" start >> "$LOG" 2>&1 || true
  fi
  exit 1
fi
