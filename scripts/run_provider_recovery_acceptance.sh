#!/usr/bin/env bash
set -euo pipefail

# Provider recovery acceptance contract:
# provider effect -> response loss -> durable uncertain intent -> restart
# -> provider read-back -> one settlement and zero replayed provider calls.

state_dir="$(mktemp -d)"
container_name="charterforge-provider-recovery-$$"
trap 'docker rm -f "$container_name" >/dev/null 2>&1 || true' EXIT

docker build --tag charterforge:provider-recovery . >/dev/null
docker run --detach --name "$container_name" \
  -e HERMES_HOME=/opt/data -e CHARTERFORGE_HOME=/opt/data \
  -v "$state_dir:/opt/data" charterforge:provider-recovery >/dev/null

docker exec "$container_name" python3 \
  /opt/hermes/scripts/provider_recovery_acceptance.py interrupt
docker restart "$container_name" >/dev/null
docker exec "$container_name" python3 \
  /opt/hermes/scripts/provider_recovery_acceptance.py recover

echo "provider recovery acceptance: PASS"
