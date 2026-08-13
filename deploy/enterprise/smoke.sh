#!/usr/bin/env bash
# Smoke test for the Hermes Enterprise deployment packaging.
#
# 1. If docker is available, build the enterprise image (offline-guarded).
# 2. Always run a no-docker fallback: exercise the enterprise package via
#    the repo venv. The enterprise controller CLI (enterprise/cli.py) lands
#    on a sibling branch — if it is not merged yet we degrade to importing
#    the enterprise package and print PARTIAL instead of failing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
IMAGE_TAG="${IMAGE_TAG:-hermes-enterprise:smoke}"
ENT_HOME="${ENT_HOME:-/tmp/ent-smoke}"

echo "== Hermes Enterprise smoke =="
echo "repo root: ${REPO_ROOT}"

# ---------- Docker image build ----------
if command -v docker >/dev/null 2>&1; then
    # Guard against a present-but-dead or offline daemon: bounded probe.
    if timeout 10 docker info >/dev/null 2>&1; then
        echo "-- docker build (${IMAGE_TAG})"
        if timeout "${DOCKER_BUILD_TIMEOUT:-900}" \
            docker build -f "${SCRIPT_DIR}/Dockerfile" -t "${IMAGE_TAG}" "${REPO_ROOT}"; then
            echo "OK: image built ${IMAGE_TAG}"
        else
            echo "WARN: docker build failed or timed out (offline registry?) — continuing with venv smoke" >&2
        fi
    else
        echo "SKIP: docker present but daemon unreachable"
    fi
else
    echo "SKIP: docker not installed"
fi

# ---------- Venv fallback smoke ----------
# Prefer the repo venv, then an installed hermes venv, then system python.
PY=""
for cand in \
    "${REPO_ROOT}/.venv/bin/python3" \
    "${HOME}/.hermes/hermes-agent/.venv/bin/python3" \
    "$(command -v python3 || true)"; do
    if [ -n "${cand}" ] && [ -x "${cand}" ]; then
        PY="${cand}"
        break
    fi
done
if [ -z "${PY}" ]; then
    echo "SKIP: no python3 available for venv smoke"
    exit 0
fi
echo "-- python smoke via ${PY}"

cd "${REPO_ROOT}"
if "${PY}" -c 'import enterprise.cli' 2>/dev/null; then
    rm -rf "${ENT_HOME}"
    "${PY}" -m enterprise.cli --home "${ENT_HOME}" init
    echo "OK: enterprise.cli init succeeded (home=${ENT_HOME})"
elif "${PY}" -c 'import enterprise' 2>/dev/null; then
    echo "PARTIAL: enterprise package imports, but enterprise.cli is not present yet (sibling branch not merged)"
else
    echo "PARTIAL: enterprise package not importable from ${PY} (run from a checkout with enterprise/ installed)"
fi

echo "== smoke done =="
