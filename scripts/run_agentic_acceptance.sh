#!/usr/bin/env bash
set -euo pipefail

# Current-tree acceptance contract:
# install -> bootstrap -> blocked readiness -> satisfy controls -> ready
# -> bounded CEO worker -> container restart -> durable recovery/idempotency.

artifact_dir="$(mktemp -d)"
state_dir="$(mktemp -d)"
container_name="charterforge-agentic-acceptance-$$"
trap 'docker rm -f "$container_name" >/dev/null 2>&1 || true' EXIT

uv build --wheel --out-dir "$artifact_dir" >/dev/null
uv venv "$artifact_dir/venv" >/dev/null
uv pip install --python "$artifact_dir/venv/bin/python" \
  "$artifact_dir"/charterforge-*.whl >/dev/null
"$artifact_dir/venv/bin/charterforge" --version

docker build --tag charterforge:agentic-acceptance . >/dev/null
docker run --detach --name "$container_name" \
  -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:agentic-acceptance >/dev/null

docker exec "$container_name" python3 \
  /opt/hermes/scripts/agentic_acceptance.py prepare
docker exec "$container_name" python3 \
  /opt/hermes/scripts/agentic_acceptance.py run
docker restart "$container_name" >/dev/null
docker exec "$container_name" python3 \
  /opt/hermes/scripts/agentic_acceptance.py recover

echo "current-tree agentic acceptance: PASS"
