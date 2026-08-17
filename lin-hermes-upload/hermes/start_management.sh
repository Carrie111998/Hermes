#!/bin/sh
set -eu

: "${HERMES_DASHBOARD_INTERNAL_TOKEN:?HERMES_DASHBOARD_INTERNAL_TOKEN is required}"
: "${HERMES_DASHBOARD_UPSTREAM:?HERMES_DASHBOARD_UPSTREAM is required}"

exec sh -c 'cd lin-hermes-upload/hermes && exec uv run uvicorn management_gateway:app --host 0.0.0.0 --port "${PORT:-10000}"'
