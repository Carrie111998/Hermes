#!/usr/bin/env bash
# Targeted, serial validation for the Hermes-managed OpenAI Codex broker.
#
# This is intentionally not the full Hermes suite: it exercises the changed
# proxy adapter, private-auth boundary, local HTTP forwarding, refresh retry,
# and method/path restrictions without starting unrelated optional providers.
# Keep it serial on a shared host so validation cannot starve a live gateway.
set -euo pipefail

if (($# != 0)); then
  echo "usage: scripts/run_codex_broker_tests.sh" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/run_tests.sh" -j 1 tests/hermes_cli/test_proxy.py
