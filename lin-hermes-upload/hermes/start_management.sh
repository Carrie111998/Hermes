#!/bin/sh
set -eu

: "${HERMES_DASHBOARD_INTERNAL_TOKEN:?HERMES_DASHBOARD_INTERNAL_TOKEN is required}"

export HERMES_DASHBOARD_UPSTREAM="http://127.0.0.1:9119"
unset HERMES_WEB_DIST HERMES_SERVE_HEADLESS HERMES_DESKTOP

PROJECT_ROOT=$(pwd)

# Keep the Dashboard as a supervised child of the Management Gateway. Starting
# the gateway without a ready Dashboard produces misleading edge 502 responses.
uv run --project "$PROJECT_ROOT" --no-sync hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build 2>&1 &
dashboard_pid=$!
cleanup() {
    kill "$dashboard_pid" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

ready=0
for _ in $(seq 1 60); do
    if curl --silent --show-error --fail --max-time 2 http://127.0.0.1:9119/ >/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "$dashboard_pid" 2>/dev/null; then
        echo "Hermes Dashboard exited before becoming ready" >&2
        exit 1
    fi
    sleep 1
done

if [ "$ready" -ne 1 ]; then
    echo "Hermes Dashboard did not become ready on 127.0.0.1:9119" >&2
    exit 1
fi

cd lin-hermes-upload/hermes
exec uv run --project "$PROJECT_ROOT" uvicorn management_gateway:app --host 0.0.0.0 --port "${PORT:-10000}"
