#!/usr/bin/env bash
#
# Install a built distribution (wheel or sdist) into a clean virtualenv and
# prove the interfaze-api service works from it.
#
# The point is isolation. Running the API out of the source tree only proves
# the repo works; it says nothing about what `pip install hermes-agent[web,
# interfaze]` actually gets. Anything that lives in the repo but is absent from
# [tool.setuptools.packages.find] / package-data / MANIFEST.in still imports
# fine from a checkout and ImportErrors for every real user. So this script
# runs the server from a scratch directory with no repo checkout on sys.path.
#
# Usage:
#   scripts/ci/interfaze_packaging_check.sh <dist-file> <venv-dir> <port>
#
# Env:
#   PYTHON_VERSION  interpreter for the venv (default 3.13)
#
# Exits non-zero with the reason on any failure.

set -euo pipefail

DIST="${1:?usage: interfaze_packaging_check.sh <dist-file> <venv-dir> <port>}"
VENV="${2:?usage: interfaze_packaging_check.sh <dist-file> <venv-dir> <port>}"
PORT="${3:?usage: interfaze_packaging_check.sh <dist-file> <venv-dir> <port>}"
PYTHON_VERSION="${PYTHON_VERSION:-3.13}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
smoke="${repo_root}/scripts/ci/interfaze_api_smoke.sh"

[ -f "${DIST}" ] || { echo "::error::no such distribution: ${DIST}"; exit 1; }
DIST="$(cd "$(dirname "${DIST}")" && pwd)/$(basename "${DIST}")"

echo "==> ${DIST}"

rm -rf "${VENV}"
uv venv --python "${PYTHON_VERSION}" "${VENV}"

# PEP 508 direct reference (`name[extras] @ file://…`) rather than
# `<path>[extras]`, which is a pip-ism that not every frontend parses the same
# way. `--no-cache` keeps a previously-built wheel of the same version from
# masking a packaging regression in the distribution under test.
uv pip install --no-cache --python "${VENV}/bin/python" \
  "hermes-agent[web,interfaze] @ file://${DIST}"

# Scratch cwd: no server/, tools/, or run_agent.py reachable from '.'.
workdir="$(mktemp -d)"
home_dir="${workdir}/hermes-home"
mkdir -p "${home_dir}"
cleanup() {
  if [ -n "${api_pid:-}" ]; then
    kill "${api_pid}" 2>/dev/null || true
    wait "${api_pid}" 2>/dev/null || true
  fi
  rm -rf "${workdir}"
}
trap cleanup EXIT

export PATH="${VENV}/bin:${PATH}"
export HERMES_HOME="${home_dir}"
export INTERFAZE_DATABASE_PATH="${workdir}/interfaze/interfaze.db"
export INTERFAZE_PUBLIC_BASE_URL="http://127.0.0.1:${PORT}"
export INTERFAZE_BOOTSTRAP_ADMIN_EMAIL="ci-admin@interfaze.invalid"
export INTERFAZE_BOOTSTRAP_ADMIN_PASSWORD="ci-smoke-test-password"
# Never let a packaged boot reach a real provider.
export OPENROUTER_API_KEY="" OPENAI_API_KEY="" NOUS_API_KEY="" ANTHROPIC_API_KEY=""

echo "==> console scripts"
command -v interfaze-api
command -v hermes
# Both must resolve *inside* the venv, not to some other install on PATH.
case "$(command -v interfaze-api)" in
  "${VENV}/bin/"*) ;;
  *) echo "::error::interfaze-api resolved outside ${VENV}"; exit 1 ;;
esac
case "$(command -v hermes)" in
  "${VENV}/bin/"*) ;;
  *) echo "::error::hermes resolved outside ${VENV}"; exit 1 ;;
esac

# Everything from here runs from the scratch dir, so nothing resolves out of
# the checkout by accident. ${smoke} was absolutised above.
cd "${workdir}"

echo "==> packaged imports and data files"
"${VENV}/bin/python" - <<'PY'
import importlib.util
import sys
from pathlib import Path

# The modules the API process and its skills-sync CMD actually touch.
import server.app  # noqa: F401
import server.api_cli  # noqa: F401
import tools.skills_sync  # noqa: F401
import hermes_cli.main  # noqa: F401

failures = []

# Data files declared in pyproject that a wheel silently drops when the
# corresponding package-data / data-files entry regresses.
webui = Path(server.app.__file__).resolve().parent / "webui"
if not (webui / "index.html").is_file():
    failures.append(f"dashboard SPA missing from the distribution: {webui}/index.html")

locales = Path(sys.prefix) / "locales"
if not any(locales.glob("*.yaml")):
    failures.append(f"i18n catalogs missing from the distribution: {locales}/*.yaml")

# Lead-research reference data. registry.build_registry() runs inside
# create_app(), so a distribution missing this cannot boot the API at all.
from server.lead_research.sectors import REFERENCE_DIR

for name in ("provider-catalog.yaml", "feature-playbooks.yaml", "sectors.yaml"):
    if not (REFERENCE_DIR / name).is_file():
        failures.append(f"lead-research reference data missing: {REFERENCE_DIR / name}")
print("lead-research reference dir ->", REFERENCE_DIR)

# The interfaze extra's web-research stack. Without these the lead-discovery
# skill has no search backend and the model answers from memory.
for mod in ("ddgs", "scrapling", "markdownify", "asyncpg"):
    if importlib.util.find_spec(mod) is None:
        failures.append(f"interfaze extra did not install {mod}")

if failures:
    for line in failures:
        print(f"::error::{line}")
    raise SystemExit(1)
print("packaged imports and data files OK")
PY

echo "==> boot"
interfaze-api --host 127.0.0.1 --port "${PORT}" > "${workdir}/api.log" 2>&1 &
api_pid=$!

if ! SMOKE_PID="${api_pid}" "${smoke}" "http://127.0.0.1:${PORT}" 60; then
  echo "::group::server log"
  cat "${workdir}/api.log" || true
  echo "::endgroup::"
  exit 1
fi

echo "==> $(basename "${DIST}") OK"
