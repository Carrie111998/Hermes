#!/bin/sh
set -eu

: "${HERMES_DASHBOARD_INTERNAL_TOKEN:?HERMES_DASHBOARD_INTERNAL_TOKEN is required}"

export HERMES_DASHBOARD_UPSTREAM="${HERMES_DASHBOARD_UPSTREAM:-http://127.0.0.1:9119}"

cd "$(dirname "$0")/../.."

uv run hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build &
dashboard_pid=$!
trap 'kill "$dashboard_pid" 2>/dev/null || true' INT TERM EXIT

cd lin-hermes-upload/hermes
exec uv run uvicorn management_gateway:app --host 0.0.0.0 --port "${PORT:-10000}"
