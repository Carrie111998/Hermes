#!/usr/bin/env bash
# Smoke-drives the Hermes Agent CLI: build, version/doctor checks,
# a one-shot agent invocation (with and without a provider key), and
# a quick pytest subset. Run from the repo root (git-bash / WSL / Linux).
#
# Why this file exists instead of just typing the commands: this repo
# lives under a OneDrive-synced path on the reference machine, and
# `uv sync` intermittently fails there (see UV_PROJECT_ENVIRONMENT
# below) — this script encodes the fix so it isn't rediscovered every run.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../../.." # repo root

# uv's package-replace step collides with OneDrive's file-lock-on-sync
# behavior ("error: failed to remove directory ... Access is denied.
# (os error 5)") when .venv lives inside a OneDrive folder. Relocating
# the venv outside OneDrive fixes it. Harmless (and unnecessary) if
# your checkout isn't under OneDrive.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/hermes-agent}"
mkdir -p "$(dirname "$UV_PROJECT_ENVIRONMENT")"

echo "== uv python pin =="
uv python pin 3.12.13

echo "== uv sync (base + dev extras) =="
uv sync --extra dev

echo "== hermes --version =="
uv run hermes --version

echo "== hermes doctor =="
uv run hermes doctor || true # exits 0 normally; `|| true` only guards flaky CI hosts

echo "== one-shot invocation, no provider configured (expect exit 1, clear error) =="
set +e
uv run hermes -z "say hi"
echo "exit: $?"
set -e

echo "== one-shot invocation, bogus provider key (expect exit 0, upstream 401 text as the 'response') =="
set +e
OPENROUTER_API_KEY=not-a-real-key \
  uv run hermes -z "say hi"
echo "exit: $?"
set -e

echo "== pytest smoke subset (skip anything symlink-based — see Gotchas) =="
uv run pytest tests/test_account_usage.py -q

echo "== done =="
