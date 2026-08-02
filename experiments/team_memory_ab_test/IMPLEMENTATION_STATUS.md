# Stage 1 Implementation Status

## Complete

- Configuration backup exists locally under `backups/` and is ignored by Git.
- `plugins/team_memory` is a standard opt-in Hermes plugin.
- SQLite schema migration and FTS5 rebuild are implemented.
- The earlier candidate schema (`created_by`/`metadata`) migrates to `author`
  without returning legacy metadata to the Agent.
- Workspace/project scope, review status, source metadata, expiry, CJK fallback,
  bounded results, separate query metrics, and explicit uninstall confirmation exist.
- `team_memory_search` is registered only through `PluginContext` and hidden by
  `check_fn` when the feature is disabled or scope is missing.
- Operator CLI is `hermes team-memory`; existing `hermes memory` remains unchanged.
- Real Agent A/B runner uses isolated processes, deterministic arm-order rotation,
  two repetitions by default (40 paired runs), credential scrubbing, and refuses
  an undersampled decision.

## Verified

```text
tests/plugins/test_team_memory_plugin.py + tests/experiments/test_team_memory_ab_test.py: 9 passed
quick_test.sh with Hermes venv: passed
CLI init/add/search/status in a temporary HERMES_HOME: passed
English and Chinese search: passed
Old FTS terms after replacement and deletion: absent
Same-day expired entries: excluded from Agent search, visible to operator audit
Legacy `created_by` schema migration: passed
Cross-workspace query: empty
Feature flag off: tool schema absent
```

## Not yet a production claim

No model-backed A/B experiment has been run in this change. Do not enable the
feature in a production profile until a real source profile, task set, cost
budget, and rollback owner are explicitly approved.
