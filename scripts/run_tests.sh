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
#   * An explicit isolated interpreter override via
#     ``--python /absolute/executable``. The override must import pytest and is
#     intended for dependency-incompatible security suites only.
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh --python /tmp/test-venv/bin/python tests/foo.py
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Canonical-runner-only options ──────────────────────────────────────────
# Strip the isolated Python override before forwarding all remaining args to
# run_tests_parallel.py. This is intentionally a CLI option, not a HERMES_*
# environment/config surface.
TEST_PYTHON=""
FORWARDED_ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --)
      # Everything after the canonical separator belongs to pytest. Never
      # reinterpret a passthrough `--python` as a runner option.
      FORWARDED_ARGS+=("$@")
      set --
      ;;
    --python)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "error: --python requires an absolute executable path" >&2
        exit 1
      fi
      if [ -n "$TEST_PYTHON" ]; then
        echo "error: --python may be specified only once" >&2
        exit 1
      fi
      TEST_PYTHON="$2"
      shift 2
      ;;
    --python=*)
      if [ -n "$TEST_PYTHON" ]; then
        echo "error: --python may be specified only once" >&2
        exit 1
      fi
      TEST_PYTHON="${1#--python=}"
      if [ -z "$TEST_PYTHON" ]; then
        echo "error: --python requires an absolute executable path" >&2
        exit 1
      fi
      shift
      ;;
    *)
      FORWARDED_ARGS+=("$1")
      shift
      ;;
  esac
done
set -- "${FORWARDED_ARGS[@]}"

# ── Locate python ───────────────────────────────────────────────────────────
# An explicitly selected isolated test interpreter takes precedence. This
# keeps dependency-incompatible suites (for example WebAuthn 3 / cryptography
# 49) outside Hermes' production dependency graph. The absolute-path and
# import checks make the override fail closed.
if [ -n "$TEST_PYTHON" ]; then
  case "$TEST_PYTHON" in
    /*) ;;
    *)
      echo "error: --python must be an absolute path" >&2
      exit 1
      ;;
  esac
  if [ ! -x "$TEST_PYTHON" ] \
      || ! "$TEST_PYTHON" -I -c 'import pytest' 2>/dev/null; then
    echo "error: --python must be executable and import pytest" >&2
    exit 1
  fi
  PYTHON="$TEST_PYTHON"
  echo "▶ using explicit isolated test Python: $PYTHON"
else
  # Probe local venvs first; fall back to the Nix devShell's editable venv
  # (HERMES_PYTHON is exported by the devShell hook and ships [dev] extras:
  # pytest, pytest-asyncio, pytest-timeout, ruff, ty).
  #
  # A candidate must have pytest INSTALLED, not merely exist. The release venv
  # at ~/.hermes/hermes-agent/venv has bin/activate but no pytest, so an
  # existence-only probe selected it in checkouts/worktrees without a local
  # .venv — every file then died with "No module named pytest" and the run
  # reported "0 tests passed" (which reads green at a glance even though the
  # exit code is 1). Skip such a venv and keep probing instead.
  VENV=""
  VENV_PYTHON=""
  SKIPPED_VENVS=""
  for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
    if [ -f "$candidate/bin/activate" ]; then
      if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
        VENV="$candidate"
        VENV_PYTHON="$candidate/bin/python"
        break
      fi
      SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
    fi
    # Native Windows venv layout: python.exe and activate live under
    # Scripts/, and there is no bin/.
    if [ -f "$candidate/Scripts/activate" ]; then
      if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
        VENV="$candidate"
        VENV_PYTHON="$candidate/Scripts/python.exe"
        break
      fi
      SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
    fi
  done

  if [ -n "$SKIPPED_VENVS" ]; then
    for skipped in $SKIPPED_VENVS; do
      echo "▶ skipping venv without pytest: $skipped" >&2
    done
  fi

  if [ -n "$VENV" ]; then
    PYTHON="$VENV_PYTHON"
  elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
      && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
    # Guard with an import check: HERMES_PYTHON may point at the RELEASE
    # venv (no pytest) when inherited from a wrapped `hermes` binary rather
    # than the devShell hook.
    PYTHON="$HERMES_PYTHON"
    echo "▶ no local venv — using Nix dev venv via HERMES_PYTHON: $PYTHON"
  else
    echo "error: no virtualenv with pytest found in $REPO_ROOT/.venv or $REPO_ROOT/venv," >&2
    echo "       and HERMES_PYTHON is not a python with pytest (enter the Nix devShell or create a venv)" >&2
    if [ -n "$SKIPPED_VENVS" ]; then
      echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
    fi
    exit 1
  fi
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi

# Keep pytest's temporary files in the invoking user's private temp root.
# macOS assigns files created directly under /private/tmp to wheel (gid 0),
# while launchd provides a per-user TMPDIR owned by the caller's primary
# group. Dropping TMPDIR at the env -i boundary therefore changes filesystem
# identity semantics and makes owner/group-sensitive tests fail only under the
# canonical runner. Resolve the directory before the environment is cleared;
# this carries a path, never credential material.
RUNNER_TMPDIR="${TMPDIR:-}"
if [ -z "$RUNNER_TMPDIR" ] && [ "$(uname -s)" = "Darwin" ]; then
  RUNNER_TMPDIR="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || true)"
fi
if [ -z "$RUNNER_TMPDIR" ]; then
  RUNNER_TMPDIR="/tmp"
fi
case "$RUNNER_TMPDIR" in
  /*) ;;
  *)
    echo "error: TMPDIR must be an absolute writable directory" >&2
    exit 1
    ;;
esac
if [ ! -d "$RUNNER_TMPDIR" ] || [ ! -w "$RUNNER_TMPDIR" ]; then
  echo "error: TMPDIR must be an absolute writable directory" >&2
  exit 1
fi
RUNNER_TMPDIR="$(cd "$RUNNER_TMPDIR" && pwd -P)"

# Native Windows CPython resolves user, platform, socket, and temporary paths
# through these location variables. They carry paths rather than credentials.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done

# ── Test-runner knobs (computed before we drop env) ────────────────────────
# The runner's own documented environment knobs must survive the hermetic
# `env -i` below, or they are silent no-ops for anyone invoking this script:
#
#   * HERMES_TEST_WORKERS / PATHS / FILE_TIMEOUT / FILE_RETRIES / SLICE are
#     read by run_tests_parallel.py at argparse-default time — inside the
#     stripped environment.
#   * HERMES_TEST_IMAGE is read by tests/docker/conftest.py to skip its
#     session-scoped `docker build`. CI's docker.yml sets it to the image
#     the build step just loaded; stripping it made every per-file pytest
#     subprocess rebuild the 5GB image from a cold builder cache instead
#     (~4 min per worker per run, and the rebuilt image lacked the
#     HERMES_GIT_SHA build-arg the workflow bakes in).
#
# These are test-infrastructure knobs, not credentials — same class as the
# HERMES_RUN_SLOW_PET_TESTS / HERMES_E2E_BROWSER opt-ins already forwarded.
# Keep this an explicit allowlist (no HERMES_TEST_* glob) so the "no
# credential can leak" property stays auditable at a glance.
TEST_ENV=()
for _test_var in HERMES_TEST_IMAGE HERMES_TEST_WORKERS HERMES_TEST_PATHS \
  HERMES_TEST_FILE_TIMEOUT HERMES_TEST_FILE_RETRIES HERMES_TEST_SLICE; do
  if [ -n "${!_test_var:-}" ]; then
    TEST_ENV+=("$_test_var=${!_test_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  ${TEST_ENV[@]+"${TEST_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  TMPDIR="$RUNNER_TMPDIR" \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${MUNCHO_OWNER_GATE_ISOLATED_TEST_RUNTIME:+MUNCHO_OWNER_GATE_ISOLATED_TEST_RUNTIME="$MUNCHO_OWNER_GATE_ISOLATED_TEST_RUNTIME"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
