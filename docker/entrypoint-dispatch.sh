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
# theirs. Mark only a gateway process that the wrapper launches directly;
# descendants inherit environment markers, so takeover authorization remains
# on the explicit CLI flag rather than ambient state.
if [ "${1:-}" = "gateway" ]; then
    if [ "$#" -eq 1 ]; then
        exec "$WRAPPER" gateway run --external-supervisor
    elif [ "${2:-}" = "run" ]; then
        exec "$WRAPPER" "$@" --external-supervisor
    fi
elif [ "${1:-}" = "hermes" ] && [ "${2:-}" = "gateway" ]; then
    if [ "$#" -eq 2 ]; then
        exec "$WRAPPER" hermes gateway run --external-supervisor
    elif [ "${3:-}" = "run" ]; then
        exec "$WRAPPER" "$@" --external-supervisor
    fi
elif { [ "${1:-}" = "--profile" ] || [ "${1:-}" = "-p" ]; } && \
     [ "${3:-}" = "gateway" ]; then
    if [ "$#" -eq 3 ]; then
        exec "$WRAPPER" "$@" run --external-supervisor
    elif [ "${4:-}" = "run" ]; then
        exec "$WRAPPER" "$@" --external-supervisor
    fi
else
    case "${1:-}" in
        --profile=*)
            if [ "${2:-}" = "gateway" ]; then
                if [ "$#" -eq 2 ]; then
                    exec "$WRAPPER" "$@" run --external-supervisor
                elif [ "${3:-}" = "run" ]; then
                    exec "$WRAPPER" "$@" --external-supervisor
                fi
            fi
            ;;
    esac
fi

exec "$WRAPPER" "$@"
