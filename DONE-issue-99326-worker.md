# DONE issue 99326 worker

goal_completed: true
issue: https://github.com/NousResearch/hermes-agent/issues/99326
implementation_commit: b575582f4c64fc30396ab8e4fb490271b1aa9fa2

## Files

- `hermes_cli/kanban_db.py`
- `hermes_cli/kanban.py`
- `hermes_cli/kanban_swarm.py`
- `tools/kanban_tools.py`
- `plugins/kanban/dashboard/plugin_api.py`
- `plugins/kanban/dashboard/dist/index.js`
- focused tests under `tests/hermes_cli`, `tests/tools`, and `tests/plugins`

## RED

Independent scratch baseline on `origin/main` accepted both reasonless paths:

```text
initial_status=blocked
runtime_block_accepted=True
runtime_status=blocked
negative_control=RED_CONFIRMED
```

The Desktop bundle baseline lacked all three current-reason markers; the candidate contained all three.

## GREEN

```text
167 passed, 1 skipped, 1 warning in 78.99s
Ruff: All checks passed!
node --check: PASS
Python compileall: PASS
candidate_control=PASS
```

Candidate control proved:

- reasonless initial blocked creation rejected;
- reasonless runtime block rejected;
- direct reasonless SQL transition rejected;
- explicit reason persisted;
- unblock cleared current reason;
- immutable blocked event history preserved.

## ACTUALLY_USED

- Capability IDs 1858 and 1886 for the durable worker-verifier-lander graph.
- Coin `champion_challenger`: Kimi challenger then Codex champion.
- Parent bare-hands code inspection, TDD controls and focused test execution.

## DISCOVERED

- Kimi run 113 was heartbeat-only with empty logs and zero edits; run 116 crashed; run 117 raced reassignment and was reclaimed.
- Codex run 118 produced the patch but exhausted 150/150 iterations before tests/commit/report.
- Another default-profile session stopped the dispatcher during run 116, causing cross-session interference.
- The current reason must be persisted in `tasks`; event/run history alone is not an authoritative current-state surface.
- SQLite enforcement triggers require rollback to drop the three triggers before reverting to old code.

## UNTOUCHED

- No credentials, browser sessions, profile config, service state, live Kanban data, upstream branch or PR merge.

## MISSING

- Capability map semantic coverage for the new end-to-end blocked-reason capability remained UNKNOWN before this run.
- Background terminal watcher notifications were unusable because interactive zsh launched AQ before command-local environment guards.

## FRICTION

- Three Kimi runs yielded no usable implementation.
- Codex exhausted its iteration budget without committing.
- Parent takeover was required to inspect, test, add Desktop contract coverage and commit.
