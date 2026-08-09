#!/usr/bin/env bash
#
# Boot smoke test for the interfaze-api service.
#
# Waits for /health to answer, asserts the payload, and confirms the OpenAPI
# schema is served. Deliberately transport-only: it does not care whether the
# server is a console script on the runner, a wheel in a throwaway venv, or a
# container — .github/workflows/interfaze-api.yml points it at all three, and
# every one of them has to satisfy the same contract.
#
# Usage:
#   scripts/ci/interfaze_api_smoke.sh <base-url> [timeout-seconds]
#
# Env:
#   SMOKE_PID  optional. PID of the server process. When set, the poll loop
#              aborts early (instead of burning the full timeout) if that
#              process dies before /health comes up.
#
# Exits non-zero with the reason on any failure.

set -euo pipefail

BASE_URL="${1:?usage: interfaze_api_smoke.sh <base-url> [timeout-seconds]}"
TIMEOUT="${2:-60}"
BASE_URL="${BASE_URL%/}"

health_json="$(mktemp)"
trap 'rm -f "${health_json}"' EXIT

echo "==> waiting up to ${TIMEOUT}s for ${BASE_URL}/health"

ready=""
for _ in $(seq 1 "${TIMEOUT}"); do
  # stderr suppressed: "connection refused" is the expected state while the
  # server is still coming up, and one line per second buries the real error.
  if curl -fsS --max-time 3 "${BASE_URL}/health" -o "${health_json}" 2>/dev/null; then
    ready=1
    break
  fi
  if [ -n "${SMOKE_PID:-}" ] && ! kill -0 "${SMOKE_PID}" 2>/dev/null; then
    echo "::error::interfaze-api (pid ${SMOKE_PID}) exited before /health became reachable"
    exit 1
  fi
  sleep 1
done

if [ -z "${ready}" ]; then
  echo "::error::${BASE_URL}/health did not respond within ${TIMEOUT}s"
  exit 1
fi

python3 - "${health_json}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
print("GET /health ->", json.dumps(payload, sort_keys=True))

assert payload["status"] == "ok", payload
assert payload["service"] == "interfaze-agent", payload
assert payload["api_version"] == "v1", payload
# False here means the `hermes` CLI is missing from PATH, which is the exact
# regression that would ship a build where every agent run fails at Popen time.
assert payload["agent_runs_enabled"] is True, payload
PY

echo "==> GET ${BASE_URL}/openapi.json"
curl -fsS --max-time 5 "${BASE_URL}/openapi.json" -o /dev/null

# The dashboard SPA is package-data (server/webui/** in
# [tool.setuptools.package-data]). server/app.py only mounts it when the
# packaged webui/ directory exists, so a build that dropped it still answers
# /health and 404s here — which is the whole point of running this against the
# wheel and the image, not just the source tree.
echo "==> GET ${BASE_URL}/ (dashboard SPA)"
curl -fsS --max-time 5 "${BASE_URL}/" -o /dev/null

echo "==> smoke test passed"
