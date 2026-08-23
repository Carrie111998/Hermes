#!/usr/bin/env bash
# gateway_monitor.sh — Backup keeper for the Hermes gateway watchdog on Android Termux.
#
# Runs from cron (e.g. every 10 min).
# If the gateway AND watchdog are both dead, it relaunches the watchdog
# (which re-acquires termux-wake-lock and restarts the gateway).
set -u

HOME_DIR="${HOME:-.}"

# 1) Telegram delivery self-check
SELFCHECK_SCRIPT="$HOME_DIR/.hermes/scripts/telegram_selfcheck.sh"
if [ ! -f "$SELFCHECK_SCRIPT" ]; then
    SELFCHECK_SCRIPT="$(dirname "$0")/telegram_selfcheck.sh"
fi
bash "$SELFCHECK_SCRIPT" || true

WATCHDOG_SCRIPT="$HOME_DIR/.hermes/scripts/gateway_watchdog.sh"
if [ ! -f "$WATCHDOG_SCRIPT" ]; then
    WATCHDOG_SCRIPT="$(dirname "$0")/gateway_watchdog.sh"
fi

if pgrep -f "hermes gateway" >/dev/null 2>&1; then
    # Gateway is alive -> ensure watchdog is also running
    if [ ! -f "$HOME_DIR/.hermes/gateway_watchdog.run" ]; then
        mkdir -p "$HOME_DIR/.hermes/logs"
        setsid bash "$WATCHDOG_SCRIPT" start >> "$HOME_DIR/.hermes/logs/gateway_watchdog.log" 2>&1 &
    fi
    exit 0
fi

# Gateway dead -> relaunch watchdog (which will restart gateway)
if [ ! -f "$HOME_DIR/.hermes/gateway_watchdog.run" ]; then
    mkdir -p "$HOME_DIR/.hermes/logs"
    echo "$(date '+%Y-%m-%d %H:%M:%S') monitor: gateway and watchdog down, relaunching watchdog" >> "$HOME_DIR/.hermes/logs/gateway_watchdog.log"
    setsid bash "$WATCHDOG_SCRIPT" start >> "$HOME_DIR/.hermes/logs/gateway_watchdog.log" 2>&1 &
fi
