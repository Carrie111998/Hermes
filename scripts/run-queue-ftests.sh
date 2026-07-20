#!/bin/bash
# scripts/run-queue-ftests.sh — queue drain regression suite
#
# Runs the queue-drain functional tests (test_queue_drain_ftest.py) plus
# the sibling stack-recursion regression tests (test_pending_drain_no_recursion.py)
# and the /queue consumption tests (test_queue_consumption.py).
#
# Intended for:
#   - Manual invocation after touching gateway/run.py drain logic
#   - Post-upgrade smoke test after `git pull origin main`
#   - Daily Hermes cron (symlink at ~/.hermes/scripts/gateway-ftest.sh)
#
# Exit codes:
#   0 = all tests passed
#   1 = at least one test failed (details in stdout; suitable for Slack alert)
#
# Usage:
#   bash scripts/run-queue-ftests.sh
set -euo pipefail

# Resolve symlinks so `~/.hermes/scripts/gateway-ftest.sh -> this script`
# still lands in the hermes-agent repo root.
SCRIPT_PATH="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0")"
cd "$(dirname "$SCRIPT_PATH")/.."
source ~/.bash_profile 2>/dev/null || true

echo "=== Hermes Queue Drain Functional Tests ==="
echo "repo: $(pwd)"
echo "branch: $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
echo "head:   $(git log --oneline -1 2>/dev/null || echo unknown)"
echo ""

set +e
uv run pytest \
    tests/gateway/test_queue_drain_ftest.py \
    tests/gateway/test_pending_drain_no_recursion.py \
    tests/gateway/test_queue_consumption.py \
    -v --tb=short 2>&1 | tee /tmp/queue-ftest-output.log | tail -40
exit_code=${PIPESTATUS[0]}
set -e

if [ "$exit_code" -ne 0 ]; then
    echo ""
    echo "FAIL: queue drain tests failed — check gateway/run.py drain loop."
    echo "Full log: /tmp/queue-ftest-output.log"
    exit 1
fi

echo ""
echo "PASS: queue drain suite green."
