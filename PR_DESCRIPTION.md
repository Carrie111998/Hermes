# Stage 1 team-memory plugin: safe, reversible shared memory MVP

## Summary

This change implements the first, opt-in stage of the Xinxiang multi-agent
memory plan. It adds a standalone Hermes plugin backed by SQLite + FTS5 for
reviewed architecture decisions, API contracts, and engineering practices.
The plugin does not replace Hermes' personal memory provider or session search,
and it does not edit any Agent `SOUL.md`, `MEMORY.md`, or `USER.md` file.

## What changed

- Added `plugins/team_memory`, using the native `plugin.yaml` and
  `PluginContext` registration APIs.
- Added the read-only-for-agents `team_memory_search` tool, gated by
  `team_memory.enabled` and a validated `workspace_id`.
- Added the operator-only `hermes team-memory` CLI for initialization,
  migration, search, listing, reviewed writes, deletion, metrics, and explicit
  uninstall confirmation.
- Added workspace/project scope, review status, source metadata, stable memory
  keys, expiry timestamps, result bounds, CJK fallback, SQLite busy timeout,
  WAL, and separate metrics storage.
- Repaired FTS5 external-content update and delete behavior; initialization is
  also an idempotent rebuild/migration path for the earlier candidate schema.
- Added a real Agent A/B runner. It starts isolated Hermes `-z` processes with
  the same prompt, model inputs, and workdir. The enhanced arm alone receives
  the reviewed seed database and plugin. Default execution is 20 tasks x 2
  repetitions (40 paired runs), with deterministic arm-order alternation and a
  minimum 30-pair promotion gate.
- Added focused plugin, migration, expiry, deletion, configuration-isolation,
  and A/B analysis tests, plus an isolated CLI smoke test.
- Marked the earlier knowledge-graph/NER/ROI documents as long-term candidates,
  not current implementation or production evidence.

## Safety and rollback

- The plugin is disabled by default and must be enabled per profile.
- A running conversation keeps its existing tool snapshot; disabling the flag
  takes effect for a new Hermes process/session and does not mutate an active
  prompt prefix.
- Shared storage is explicit: profiles share data only when both
  `database_path` and `workspace_id` match.
- Agents cannot write team memory. Operator writes are reviewed CLI operations.
- A/B runs copy credentials only for the child process and remove temporary
  `auth.json`/`.env` files when each arm exits. The source profile is never
  modified.
- Local configuration backups are under `backups/` and are ignored by Git;
  they must not be staged because they contain `auth.json`.

## Verification

Verified with the Hermes runtime virtualenv:

```text
tests/plugins/test_team_memory_plugin.py
tests/experiments/test_team_memory_ab_test.py
9 passed
experiments/team_memory_ab_test/scripts/quick_test.sh
passed in a temporary HERMES_HOME; no model call performed
```

The real model-backed A/B experiment has not been run in this change. The
runner refuses to interpret an undersampled result as a promotion decision,
and no production profile has been enabled.

## Review notes

This deliberately does not add a knowledge graph, vector database, NER/RE
training pipeline, message queue, automatic agent writes, or core model tool.
Those remain separate, evidence-gated follow-ups.
