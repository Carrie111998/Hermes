#!/bin/sh
# shellcheck shell=sh
# Entry-point dispatcher for runtimes that may or may not give the image
# ownership of PID 1.
#
# Normal Docker / Podman path: this script is PID 1, so we delegate to
# s6-overlay's /init exactly as before and keep the full supervision tree.
#
# Wrapped-runtime path (Fly Machines, `docker run --init`, some Nomad/K8s
# setups): the platform's own init is already PID 1 and execs the image
# entrypoint as a child. s6-overlay aborts there with "can only run as pid 1",
# so we run the stage2 bootstrap directly and then exec the main wrapper
# without /init.

set -e

if [ "$$" -eq 1 ]; then
    exec /init /opt/hermes/docker/main-wrapper.sh "$@"
fi

STAGE2="${HERMES_ENTRYPOINT_STAGE2:-/opt/hermes/docker/stage2-hook.sh}"
WRAPPER="${HERMES_ENTRYPOINT_WRAPPER:-/opt/hermes/docker/main-wrapper.sh}"

echo "[hermes] WARNING: container entrypoint is not PID 1; skipping s6-overlay /init and falling back to direct bootstrap. Supervised services are unavailable in this runtime, but the requested command will still run." >&2
# /init normally seeds PATH with s6's helpers; the non-PID-1 fallback skips it.
export PATH="/command:/package/admin/s6/command:${PATH}"
"$STAGE2"

# The platform wrapper owns this process slot just as systemd/launchd/s6 own
# theirs. exec preserves this PID through main-wrapper and privilege drop, so
# the gateway can authorize this exact slot without trusting an ambient marker
# inherited by later child processes.
export HERMES_GATEWAY_EXTERNAL_SUPERVISOR_PID="$$"
exec "$WRAPPER" "$@"
