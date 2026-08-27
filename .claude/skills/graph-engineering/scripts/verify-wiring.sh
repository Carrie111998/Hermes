#!/usr/bin/env bash
# Prove graph-engineering is wired into the harness. Exit 0 only if every
# check prints the success token.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
ok=0
fail=0
pass() { echo "PASS $1"; ok=$((ok + 1)); }
bad() { echo "FAIL $1" >&2; fail=$((fail + 1)); }

skill="$ROOT/portable-skills/graph-engineering/SKILL.md"
if grep -q "Fake-edge test" "$skill" && grep -q "Anchors" "$skill" && grep -q "Silent node failure" "$skill"; then
  pass "skill-sections"
else
  bad "skill-sections"
fi

if grep -q "Graph engineering (not always-fan-out)" "$ROOT/rules/CODING-RULES.md"; then
  pass "coding-rule-10"
else
  bad "coding-rule-10"
fi

if grep -q "Always Fan Out Agents" "$ROOT/rules/CODING-RULES.md"; then
  bad "old-rule-10-gone"
else
  pass "old-rule-10-gone"
fi

if grep -q "graph-engineering" "$ROOT/portable-skills/fanout/SKILL.md"; then
  pass "fanout-parent"
else
  bad "fanout-parent"
fi

if grep -q "graph-engineering" "$ROOT/grok/SKILLS.md"; then
  pass "grok-daily-list"
else
  bad "grok-daily-list"
fi

wf="$ROOT/grok/workflows/graph-run.rhai"
if grep -q "let meta" "$wf" && grep -q "Silent-node gap" "$wf" && grep -q "fresh-context verifier" "$wf"; then
  pass "graph-run-workflow"
else
  bad "graph-run-workflow"
fi

if grep -q "always run the fake-edge test" "$ROOT/.claude/CLAUDE.md"; then
  pass "claude-md"
else
  bad "claude-md"
fi

if grep -q '\[AUTO\]' "$skill" && grep -q 'MUST use' "$skill"; then
  pass "skill-auto-trigger"
else
  bad "skill-auto-trigger"
fi

if grep -q 'Always run the fake-edge test' "$ROOT/rules/CODING-RULES.md" \
    && grep -q '/workflow graph-run' "$ROOT/rules/CODING-RULES.md"; then
  pass "rule-10-load"
else
  bad "rule-10-load"
fi

if grep -q 'grok/workflows' "$ROOT/install.sh"; then
  pass "install-grok-workflows"
else
  bad "install-grok-workflows"
fi

if grep -q 'HOME/.grok/skills' "$ROOT/portable-skills/install-portable-skills.sh"; then
  pass "installer-grok-skills"
else
  bad "installer-grok-skills"
fi

if grep -q 'graph-engineering' "$ROOT/bin/lib/portable-skills.sh" \
    && grep -q 'fanout' "$ROOT/bin/lib/portable-skills.sh"; then
  pass "portable-skills-set"
else
  bad "portable-skills-set"
fi

if [ -f "$ROOT/.grok/workflows/graph-run.rhai" ] && [ ! -L "$ROOT/.grok/workflows/graph-run.rhai" ] \
    && cmp -s "$ROOT/grok/workflows/graph-run.rhai" "$ROOT/.grok/workflows/graph-run.rhai"; then
  pass "project-graph-run"
else
  bad "project-graph-run"
fi

if grep -q "graph-engineering" "$ROOT/rules/generate-rules.sh"; then
  pass "generate-rules-core"
else
  bad "generate-rules-core"
fi

if grep -q '~/.grok/skills/gws' "$ROOT/grok/config.toml"; then
  pass "grok-ignore-gws"
else
  bad "grok-ignore-gws"
fi

echo "graph-engineering wiring: $ok passed, $fail failed"
if [ "$fail" -ne 0 ]; then
  exit 1
fi
echo "graph-engineering wiring verification passed"
