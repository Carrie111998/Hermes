# Hermes Durable Mission Engine P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make opted-in durable mission continuation machine-owned through typed SessionDB checkpoints, fail-closed restoration, and deterministic bounded context projection.

**Architecture:** Add a small `agent.durable_mission` module whose interface validates and projects typed checkpoint state. Extend `SessionDB` with additive `missions` and `mission_checkpoints` tables plus atomic checkpoint/session-binding methods. Thread optional `mission_id` through `AIAgent`; restore in `build_turn_context` before model context assembly, and bind the same mission durably when compression rotates sessions.

**Tech Stack:** Python 3.11, SQLite, existing `SessionDB`, Hermes turn prologue, canonical `scripts/run_tests.sh`.

**Spec:** `HERMES_DURABLE_MISSION_ENGINE_P1_IMPLEMENTATION_V1` operator mission.

## Global Constraints

- Optional `mission_id` preserves ordinary non-durable Hermes behavior.
- Missing, corrupt, incompatible, or inconsistent required checkpoints block before provider invocation.
- External authorities remain reference-only and authoritative.
- No ActionCommitStore, replay/idempotency, provider changes, approval/safety/financial redesign, or compression redesign.
- No provider calls, PLAY, campaign mutation, deployment, or secret exposure.

### Task 1: Define typed durable mission interface and failing tests

**Files:**
- Create: `agent/durable_mission.py`
- Create: `tests/agent/test_durable_mission.py`

**Interface:** `MissionCheckpoint`, `MissionStateError`, `validate_checkpoint`, `render_mission_projection`. Checkpoint fields are explicit; collection fields use bounded JSON only at the storage adapter.

- [ ] Write tests for valid ACTIVE/BLOCKED/TERMINAL states, invalid combinations, schema mismatch, deterministic projection, and summary-overrides-checkpoint rejection.
- [ ] Run `scripts/run_tests.sh -j 1 tests/agent/test_durable_mission.py`; expect import/API failures.
- [ ] Implement immutable dataclass validation and deterministic projection.
- [ ] Re-run focused tests; expect all pass.

### Task 2: Add additive SessionDB mission schema and persistence

**Files:**
- Modify: `hermes_state.py` schema and `SessionDB` methods
- Modify: `tests/agent/test_durable_mission.py`

**Interface:** `create_mission`, `get_mission_for_session`, `get_mission`, `write_mission_checkpoint`, `load_mission_checkpoint`, `bind_mission_session`, `rotate_mission_session`.

- [ ] Add typed `missions` and `mission_checkpoints` tables and indexes to `SCHEMA_SQL`.
- [ ] Test fresh startup, legacy migration, idempotent startup, mission/session distinction, checkpoint parent lineage, reopen survival, and atomic mission-current-checkpoint update.
- [ ] Run tests red against absent methods/schema.
- [ ] Implement methods using `_execute_write`; checkpoint insert and mission pointer update share one transaction.
- [ ] Add explicit rollback-path documentation in the receipt/plan; no historical transcript rewrite.
- [ ] Re-run focused tests green.

### Task 3: Integrate optional mission identity and pre-LLM restore

**Files:**
- Modify: `run_agent.py`
- Modify: `agent/agent_init.py` forwarding signature/state
- Modify: `agent/turn_context.py`
- Modify: `tests/agent/test_durable_mission.py`

**Interface:** `AIAgent(..., mission_id=None)`; durable mission absent means no restore. Durable mission present resolves from explicit ID or current session and must load a valid checkpoint before `TurnContext` returns.

- [ ] Add tests proving ordinary turns do not require missions, missing/corrupt/incompatible checkpoints prevent turn context creation, and projection is present before provider-loop entry.
- [ ] Run red.
- [ ] Thread `mission_id` through initialization; resolve session-bound mission from SessionDB on restart.
- [ ] Call restore/validate/project after `_ensure_db_session` and before message/model-context assembly.
- [ ] Re-run focused tests green, including provider-spy unreachable assertions.

### Task 4: Integrate durable session rotation

**Files:**
- Modify: `agent/conversation_compression.py`
- Modify: `hermes_state.py` if rotation transaction needs a helper
- Modify: `tests/agent/test_durable_mission.py`

- [ ] Add failing tests for two session rotations preserving one durable `mission_id`, current-session binding, and post-rotation projection.
- [ ] Implement the smallest durable binding update in the existing compression split; durable binding failure must prevent continuation.
- [ ] Do not alter compression algorithm or mission checkpoint semantics.
- [ ] Re-run focused tests green.

### Task 5: Adversarial and surface-path certification

**Files:**
- Modify: `tests/agent/test_durable_mission.py`
- Modify: `tests/cli/...` or `tests/gateway/...` only if an existing narrow test seam requires a focused assertion

- [ ] Cover wrong CodeGraph project, authority references not granting authority, checkpoint precedence over summary, DB write failure, unsupported version, and checkpoint inconsistency.
- [ ] Cover CLI, TUI, and Gateway shared `build_turn_context` boundary through existing construction/import seams without provider calls.
- [ ] Run focused suite and canonical baseline regression set.

### Task 6: Review and certify

- [ ] Inspect `git diff --check`, targeted diff, and secret scan.
- [ ] Refresh CodeGraph for the canonical repository only after implementation is stable.
- [ ] Run focused, adversarial, and canonical regression commands with exact counts.
- [ ] Confirm no ActionCommitStore, provider mutation, authority collision, or compression redesign.
- [ ] Commit only intended P1 files; stop after certification, no PR/merge/P2.

## Self-review coverage

All requested P1 items map to Tasks 1-5: identity, typed schema, versioning, atomic persistence, restore gate, bounded projection, rotation continuity, fail-closed paths, ordinary conversations, external references, migration, and adversarial tests. P2 ActionCommit/idempotency and all authority redesigns are explicitly excluded.
