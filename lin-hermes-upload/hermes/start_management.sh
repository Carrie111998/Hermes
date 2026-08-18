#!/bin/sh
set -eu

: "${HERMES_DASHBOARD_INTERNAL_TOKEN:?HERMES_DASHBOARD_INTERNAL_TOKEN is required}"

export HERMES_DASHBOARD_UPSTREAM="http://127.0.0.1:9119"
unset HERMES_WEB_DIST HERMES_SERVE_HEADLESS HERMES_DESKTOP

PROJECT_ROOT=$(pwd)
DASHBOARD_STATUS=/tmp/hermes-dashboard.exit
DASHBOARD_COMMAND="uv run --project $PROJECT_ROOT --no-sync hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build"

rm -f "$DASHBOARD_STATUS"

echo "[management] cwd=$PROJECT_ROOT"
echo "[management] dashboard_command=$DASHBOARD_COMMAND"
echo "[management] dashboard_upstream=$HERMES_DASHBOARD_UPSTREAM"
echo "[management] port=${PORT:-10000}"
echo "[management] hermes_home=${HERMES_HOME:-<unset>}"
echo "[management] web_dist=${HERMES_WEB_DIST:-<unset>}"
echo "[management] serve_headless=${HERMES_SERVE_HEADLESS:-<unset>}"
echo "[management] desktop=${HERMES_DESKTOP:-<unset>}"
echo "[management] uv=$(command -v uv || true)"
echo "[management] python=$(command -v python || true)"

# Exec the Dashboard in the background subshell so dashboard_pid is the real
# uv/Python process, not an intermediate shell. Keep both streams attached to
# Render's stdout/stderr and disable Python/uv buffering.
echo "[management] starting dashboard child"
(
    export PYTHONUNBUFFERED=1
    echo "[dashboard-child] shell_pid=$$"
    echo "[dashboard-child] cwd=$(pwd)"
    echo "[dashboard-child] exec=$DASHBOARD_COMMAND"
    exec env PYTHONUNBUFFERED=1 uv run --project "$PROJECT_ROOT" --no-sync hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build
) &
dashboard_pid=$!
echo "[management] dashboard_pid=$dashboard_pid"

cleanup() {
    if kill -0 "$dashboard_pid" 2>/dev/null; then
        echo "[management] stopping dashboard_pid=$dashboard_pid"
        kill "$dashboard_pid" 2>/dev/null || true
    fi
}
trap cleanup INT TERM EXIT

log_dashboard_process_state() {
    echo "[management] dashboard_state pid=$dashboard_pid"
    if [ -r "/proc/$dashboard_pid/cmdline" ]; then
        tr '\000' ' ' < "/proc/$dashboard_pid/cmdline"
        echo
        echo "[management] dashboard_proc_status"
        sed -n '1,40p' "/proc/$dashboard_pid/status"
    else
        echo "[management] procfs_unavailable"
    fi
    if command -v ps >/dev/null 2>&1; then
        ps -o pid,ppid,stat,etime,pcpu,pmem,args -p "$dashboard_pid" || true
    fi
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp || true
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltnp 2>/dev/null || netstat -ltn 2>/dev/null || true
    fi
}

ready=0
for _ in $(seq 1 60); do
    if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:9119/ >/dev/null; then
        ready=1
        echo "[management] dashboard_ready pid=$dashboard_pid"
        break
    fi
    if [ -f "$DASHBOARD_STATUS" ]; then
        rc=$(cat "$DASHBOARD_STATUS")
        echo "[management] dashboard_exited pid=$dashboard_pid exit_code=$rc"
        log_dashboard_process_state
        exit 1
    fi
    if kill -0 "$dashboard_pid" 2>/dev/null; then
        echo "[management] dashboard_not_ready pid=$dashboard_pid child_alive=true"
    else
        set +e
        wait "$dashboard_pid"
        rc=$?
        set -e
        echo "$rc" > "$DASHBOARD_STATUS"
        echo "[management] dashboard_exited pid=$dashboard_pid exit_code=$rc"
        log_dashboard_process_state
        exit 1
    fi
    log_dashboard_process_state
    sleep 1
done

if [ "$ready" -ne 1 ]; then
    echo "[management] dashboard_timeout pid=$dashboard_pid"
    log_dashboard_process_state
    exit 1
fi

cd lin-hermes-upload/hermes
exec uv run --project "$PROJECT_ROOT" --no-sync uvicorn management_gateway:app --host 0.0.0.0 --port "${PORT:-10000}"
