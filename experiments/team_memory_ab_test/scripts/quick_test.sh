#!/bin/bash
# Hermetic Stage 1 smoke test. It never uses the operator's HERMES_HOME.

set -eu

PYTHON_BIN="${1:-python3}"
REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
TEST_HOME="$(mktemp -d -t hermes-team-memory-smoke.XXXXXX)"
trap 'rm -rf "$TEST_HOME"' EXIT
export HERMES_HOME="$TEST_HOME"
export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$REPO_ROOT"

"$PYTHON_BIN" -m hermes_cli.main plugins enable team-memory
"$PYTHON_BIN" -m hermes_cli.main config set --force team_memory.enabled true
"$PYTHON_BIN" -m hermes_cli.main config set --force team_memory.workspace_id xinxiang
"$PYTHON_BIN" -m hermes_cli.main config set --force team_memory.database_path "$TEST_HOME/team-memory/xinxiang.db"
"$PYTHON_BIN" -m hermes_cli.main team-memory init --workspace xinxiang

"$PYTHON_BIN" "$REPO_ROOT/experiments/team_memory_ab_test/shared_memory_seed/seed_data.py" \
  --workspace xinxiang --db "$TEST_HOME/team-memory/xinxiang.db"
"$PYTHON_BIN" -m hermes_cli.main team-memory search "JWT" --workspace xinxiang
"$PYTHON_BIN" -m hermes_cli.main team-memory search "错误处理" --workspace xinxiang
"$PYTHON_BIN" -m hermes_cli.main team-memory status --workspace xinxiang

echo "Stage 1 smoke test passed. No model A/B run was performed."
echo "For a real isolated A/B run, provide an explicit source profile to:"
echo "  $PYTHON_BIN experiments/team_memory_ab_test/scripts/run_experiment.py --source-home <profile-home>"
