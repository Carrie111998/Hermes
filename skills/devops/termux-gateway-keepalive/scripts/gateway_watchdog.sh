#!/usr/bin/env bash
# gateway_watchdog.sh — Supervised, detached, wake-locked Hermes gateway keeper on Android Termux.
#
# Resolves the Android background process reclamation issue:
#   1. Acquires a termux-wake-lock (keeps device/process active).
#   2. Runs the gateway detached (setsid) so it is no longer bound to the interactive shell.
#   3. Supervises the process in a 15-second loop, restarting immediately if it dies.
#   4. Fork-bomb guard: max 10 restarts per 10-minute window with 60s backoff.
#
# Usage:
#   bash gateway_watchdog.sh start   # launch detached in background
#   bash gateway_watchdog.sh stop    # stop gateway + watchdog + release lock
#   bash gateway_watchdog.sh status  # check alive status
#
set -u

HOME_DIR="${HOME:-.}"
LOCK_FILE="$HOME_DIR/.hermes/gateway_watchdog.lock"
PID_FILE="$HOME_DIR/.hermes/gateway_watchdog.pid"
RUN_FLAG="$HOME_DIR/.hermes/gateway_watchdog.run"

# Locate hermes CLI binary dynamically
find_hermes() {
    if command -v hermes >/dev/null 2>&1; then
        command -v hermes
    elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/hermes" ]; then
        echo "$VIRTUAL_ENV/bin/hermes"
    elif [ -x "$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes" ]; then
        echo "$HOME_DIR/.hermes/hermes-agent/venv/bin/hermes"
    elif [ -x "$HOME_DIR/hermes-agent/venv/bin/hermes" ]; then
        echo "$HOME_DIR/hermes-agent/venv/bin/hermes"
    else
        echo "hermes"
    fi
}

start_gateway() {
    mkdir -p "$HOME_DIR/.hermes/logs"
    GW_BIN=$(find_hermes)
    # Detach so it survives shell closure and Termux backgrounding
    setsid "$GW_BIN" gateway >> "$HOME_DIR/.hermes/logs/gateway_stdout.log" 2>&1 &
    echo $! > "$HOME_DIR/.hermes/gateway.pid"
}

stop_all() {
    rm -f "$RUN_FLAG"
    if [ -f "$HOME_DIR/.hermes/gateway.pid" ]; then
        kill "$(cat "$HOME_DIR/.hermes/gateway.pid" 2>/dev/null)" 2>/dev/null || true
    fi
    pkill -f "hermes gateway" 2>/dev/null || true
    # Release Android wake-lock
    command -v termux-wake-unlock >/dev/null 2>&1 && termux-wake-unlock 2>/dev/null || true
    rm -f "$LOCK_FILE" "$PID_FILE" "$HOME_DIR/.hermes/gateway.pid"
    echo "stopped gateway + watchdog, released wake-lock"
}

status() {
    if pgrep -f "hermes gateway" >/dev/null 2>&1; then
        echo "gateway: ALIVE (pid $(pgrep -f 'hermes gateway' | head -1))"
    else
        echo "gateway: DEAD"
    fi
    if [ -f "$RUN_FLAG" ]; then
        echo "watchdog: RUNNING (pid $(cat "$PID_FILE" 2>/dev/null))"
    else
        echo "watchdog: not running"
    fi
}

case "${1:-start}" in
    start)
        if [ -f "$RUN_FLAG" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
            echo "watchdog already running (pid $(cat "$PID_FILE"))"; exit 0
        fi
        mkdir -p "$HOME_DIR/.hermes/logs"
        touch "$RUN_FLAG"
        echo $$ > "$PID_FILE"
        # Acquire wake-lock so Android doesn't reclaim the process
        command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock 2>/dev/null || true
        echo "watchdog started (pid $$), wake-lock held"

        attempts=0
        window_start=$(date +%s)
        while [ -f "$RUN_FLAG" ]; do
            if ! pgrep -f "hermes gateway" >/dev/null 2>&1; then
                now=$(date +%s)
                if [ $((now - window_start)) -gt 600 ]; then
                    attempts=0
                    window_start=$now
                fi
                attempts=$((attempts + 1))
                if [ $attempts -gt 10 ]; then
                    sleep 60
                    attempts=0
                    window_start=$(date +%s)
                    continue
                fi
                echo "$(date '+%Y-%m-%d %H:%M:%S') watchdog: gateway down, restart #$attempts" >> "$HOME_DIR/.hermes/logs/gateway_watchdog.log"
                start_gateway

                # Ambient notification (works offline when Telegram is down)
                NOTIFY_SCRIPT="$HOME_DIR/.hermes/scripts/presence_notify.sh"
                if [ ! -f "$NOTIFY_SCRIPT" ]; then
                    NOTIFY_SCRIPT="$(dirname "$0")/presence_notify.sh"
                fi
                bash "$NOTIFY_SCRIPT" "✅ Hermes Agent: gateway recovered automatically (restart #$attempts)." 2>/dev/null || true
            fi
            sleep 15
        done
        ;;
    stop) stop_all ;;
    status) status ;;
    *) echo "usage: $0 {start|stop|status}"; exit 1 ;;
esac
