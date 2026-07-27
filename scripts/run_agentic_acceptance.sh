#!/usr/bin/env bash
set -euo pipefail

# Current-tree acceptance contract:
# install -> bootstrap -> blocked readiness -> satisfy controls -> ready
# -> bounded CEO worker -> container restart -> durable recovery/idempotency
# -> uncertain provider effect -> second restart -> read-back convergence.
# -> master stop -> worker exits without another provider effect.

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

# Prove the installed image can launch a subordinate process from an exact
# grant, persist its evidence, wake a fresh CEO runtime, and verify the parent
# objective.  Use an isolated authority DB and board so this proof does not
# alter the primary bootstrap acceptance state.
docker exec \
  -e HERMES_DELEGATION_AUTHORITY_DB=/tmp/delegation-acceptance.db \
  -e HERMES_DELEGATION_BOARD="process-separated-acceptance-$$" \
  "$container_name" python3 \
  /opt/hermes/scripts/delegation_process_acceptance.py

# Exercise the harder failure boundary in the same installed image and state
# volume: the provider effect occurs, the response is lost, and recovery must
# reconcile by read-back after a real container restart without replaying it.
docker exec "$container_name" python3 \
  /opt/hermes/scripts/provider_recovery_acceptance.py interrupt
docker restart "$container_name" >/dev/null
docker exec "$container_name" python3 \
  /opt/hermes/scripts/provider_recovery_acceptance.py recover
docker exec "$container_name" python3 \
  /opt/hermes/scripts/agentic_acceptance.py stop

echo "current-tree agentic acceptance: PASS"
