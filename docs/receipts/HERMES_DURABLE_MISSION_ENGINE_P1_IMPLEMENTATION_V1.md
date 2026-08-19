# HERMES_DURABLE_MISSION_ENGINE_P1_IMPLEMENTATION_V1

## Verdict

`HERMES_DURABLE_MISSION_ENGINE_P1_CERTIFIED`

## Baseline

- branch: `hermes-durable-mission-engine-p1`
- starting_sha: `2402c0331a41ccf4a4d763055ace620a3bf8da3f`
- implementation_commit: `45813d02c9`
- clean: `true` after receipt commit

## Files changed

- `agent/durable_mission.py`
- `agent/agent_init.py`
- `agent/conversation_compression.py`
- `agent/turn_context.py`
- `hermes_state.py`
- `run_agent.py`
- `tests/agent/test_durable_mission.py`
- `docs/superpowers/plans/2026-08-19-hermes-durable-mission-engine-p1.md`
- this receipt

No ActionCommitStore, replay/idempotency, provider behavior, approval, safety, financial, routing, CodeGraph, convergence, or compression redesign changes.

## Database

- schema_version: checkpoint schema `1`; existing SessionDB schema numbering unchanged
- tables_added: `missions`, `mission_sessions`, `mission_checkpoints`
- migration: additive declarative `SCHEMA_SQL`; existing sessions/messages untouched
- rollback: revert implementation commit; additive tables remain inert and old Hermes code continues using existing tables

## Mission identity

- implementation: optional `AIAgent(mission_id=...)` plus SessionDB `mission_sessions`
- session_independent: `true`; mission identity is distinct from session ID
- rotation_test: two durable rotations preserve one mission ID and all session lineage resolves to it
- restart: session-bound lookup reconstructs mission identity after SessionDB reopen/process simulation

## Checkpoint

- implementation: typed `MissionCheckpoint` with explicit required fields
- storage: `mission_checkpoints`, JSON only for bounded collections/references
- atomic: checkpoint insert and `missions.current_checkpoint_id` update share `_execute_write` transaction
- versioned: `state_version` validated against checkpoint schema version `1`
- lineage: parent checkpoint must equal current checkpoint
- restart_survival: verified through SessionDB close/reopen
- state validation: ACTIVE requires `next_action`; BLOCKED requires `blocker`; TERMINAL requires `terminal_state` and forbids `next_action`

## Restore gate

- implementation: `restore_mission_for_turn`
- common_boundary: `agent/turn_context.py`, after `_ensure_db_session`, before message assembly
- CLI: reaches `run_agent.AIAgent` and shared `build_turn_context`
- TUI: reaches shared `run_conversation` and `build_turn_context`
- gateway: reaches shared `run_conversation` and `build_turn_context`
- fail-closed: missing, corrupt, incompatible, inconsistent, or CodeGraph-mismatched checkpoint raises before conversation loop/provider path

## Context projection

- implementation: deterministic `render_mission_projection`
- bounded: maximum 16 KiB; collections and fields bounded
- deterministic: same typed checkpoint produces byte-identical projection
- precedence: projection states checkpoint values as authoritative; transcript/plugin text cannot rewrite them

## Compression

- mission_id_preserved: `true`
- checkpoint_preserved: `true`; rotation uses atomic `rotate_mission_session`
- summary_authoritative: `false`
- compression algorithm changed: `false`

## External authorities

- approval: reference-only; observed status is not projected as authorization
- safety: reference-only
- financial: reference-only
- repo_router: remains external
- codegraph: project/fingerprint reference only; explicit mismatch blocks restore
- convergence: reference-only

## Tests

- focused_passed: `35`
- focused_failed: `0`
- baseline_passed: `751`
- baseline_failed_real: `0`
- blocked_infrastructure: `9` gateway async tests; unchanged missing `pytest-asyncio`, exact error `async def functions are not natively supported`
- baseline files: SessionDB, turn context, compression, tool dispatch, model tools, CLI startup, TUI server, bootstrap, MCP startup, gateway startup
- no provider calls executed

## Adversarial

- passed: missing checkpoint, corrupt checkpoint, unsupported version, invalid state, wrong parent, write failure, summary conflict, CodeGraph mismatch, authority-reference non-escalation, two session rotations, post-update projection
- failed: `0`

## Security

- secret_values_exposed: `0`

## Acceptance invariants

```text
MISSION_ID_MACHINE_OWNED=true
MISSION_ID_SURVIVES_SESSION_ROTATION=true
CHECKPOINT_MACHINE_OWNED=true
CHECKPOINT_VERSIONED=true
CHECKPOINT_ATOMIC=true
CHECKPOINT_SURVIVES_RESTART=true
NEXT_ACTION_MACHINE_OWNED=true
NEXT_ACTION_NOT_CONVERSATION_OWNED=true
PRE_LLM_RESTORE_GATE_ENFORCED=true
MISSING_CHECKPOINT_FAILS_CLOSED=true
CORRUPT_CHECKPOINT_FAILS_CLOSED=true
INCOMPATIBLE_CHECKPOINT_FAILS_CLOSED=true
CONVERSATION_CANNOT_OVERRIDE_CHECKPOINT=true
EXTERNAL_AUTHORITIES_REMAIN_AUTHORITATIVE=true
NON_DURABLE_CONVERSATIONS_UNBROKEN=true
COMPRESSION_DOES_NOT_OWN_MISSION_STATE=true
NO_PROVIDER_MUTATION=true
PLAY_EXECUTED=false
```

P2 not started. ActionCommitStore not implemented.
