#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # path + pytest args
#   scripts/run_tests.sh -- -v --tb=long            # pytest args only
#
# Everything after a literal '--' is passed through to each per-file
# pytest invocation. Positional path arguments before '--' override
# the default discovery root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Activate venv ───────────────────────────────────────────────────────────
# Probe order matters: repo-local venvs win over the shared ~/.hermes one.
# A venv only qualifies if pytest is actually importable — a venv that exists
# but was never populated (or was created for a different purpose) would
# otherwise shadow a working one later in the list and fail with a bare
# "No module named pytest".
CANDIDATES=("$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv")
VENV=""
SKIPPED_NO_PYTEST=()

for candidate in "${CANDIDATES[@]}"; do
  [ -x "$candidate/bin/python" ] || continue
  if "$candidate/bin/python" -c "import pytest" >/dev/null 2>&1; then
    VENV="$candidate"
    break
  fi
  SKIPPED_NO_PYTEST+=("$candidate")
done

if [ -z "$VENV" ]; then
  if [ ${#SKIPPED_NO_PYTEST[@]} -gt 0 ]; then
    echo "error: found virtualenv(s) without pytest installed:" >&2
    for v in "${SKIPPED_NO_PYTEST[@]}"; do
      echo "         $v" >&2
    done
    echo "       install the dev extras into one of them, e.g.:" >&2
    echo "         ${SKIPPED_NO_PYTEST[0]}/bin/python -m pip install -e '.[dev]'" >&2
  else
    echo "error: no virtualenv found. Looked in:" >&2
    for c in "${CANDIDATES[@]}"; do
      echo "         $c" >&2
    done
  fi
  exit 1
fi

if [ ${#SKIPPED_NO_PYTEST[@]} -gt 0 ]; then
  echo "note: skipped venv without pytest: ${SKIPPED_NO_PYTEST[*]}" >&2
  echo "      using $VENV" >&2
fi

PYTHON="$VENV/bin/python"


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
