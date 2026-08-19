# HERMES_DURABLE_ACTION_COMMIT_P2_IMPLEMENTATION_V1

## Verdict

`HERMES_DURABLE_ACTION_COMMIT_P2_CERTIFIED`

## Baseline

- branch: `hermes-durable-action-commit-p2`
- starting_sha: `32308e593a5f493102a61d3d7a65ab84e5e2421e`
- p1_sha: `32308e593a5f493102a61d3d7a65ab84e5e2421e`
- implementation_commit: `a42c73e341`
- clean before receipt commit: `true`

## Files changed

- `agent/action_commit.py`
- `agent/agent_runtime_helpers.py`
- `agent/conversation_loop.py`
- `agent/durable_mission.py`
- `agent/tool_executor.py`
- `hermes_state.py`
- `model_tools.py`
- `tests/agent/test_action_commit.py`
- `docs/superpowers/plans/2026-08-19-hermes-durable-action-commit-p2.md`

No new MissionEngine, checkpoint store, ActionCommitStore duplicate, compression redesign, provider behavior, approval policy, safety policy, financial policy, routing, CodeGraph, or convergence changes.

## Database

- schema_version: additive declarative SessionDB schema; action protocol v1
- tables_added: `mission_actions`
- migration: idempotent `SCHEMA_SQL`; historical sessions/messages untouched
- rollback: revert implementation commit; the additive table remains inert for P1/older Hermes code

## Action identity

- implementation: opaque machine-generated `act_<uuid>` IDs
- session_independent: `true`; mission and checkpoint are explicit foreign linkage fields
- restart_survival: `true`; verified through SessionDB close/reopen
- rotation_survival: `true`; verified through mission session rotation

## Fingerprint

- implementation: SHA-256 over canonical tool name, normalized arguments, and execution context
- canonicalization: sorted mapping keys, stable JSON encoding, deterministic scalar/list handling
- secrets_safe: `true`; raw secret values are never stored in summaries, logs, or receipts

## Action state machine

States:

`PLANNED`, `AUTHORIZED`, `RUNNING`, `COMMITTED`, `FAILED`, `UNKNOWN_OUTCOME`, `VERIFY_REQUIRED`, `REJECTED`, `SUPERSEDED`

Transition validation is machine-owned. `FAILED` is used only for explicit pre-dispatch failure or deterministic `VERIFIED_ABSENT`; uncertain post-dispatch paths become `UNKNOWN_OUTCOME`.

## Dispatch integration

- pre_execution_boundary: existing plugin/middleware/approval/safety gates, then ledger preparation
- running_persisted_before_dispatch: `true`; atomic SessionDB transition precedes registry/inline dispatch
- post_execution_boundary: result classification and durable outcome persistence
- committed_before_continuation: `true`; outcome persistence failure blocks continuation
- registry path: `model_tools.handle_function_call` wraps existing `registry.dispatch`
- inline path: shared agent execution middleware wrapper

## Replay policy

- implementation: deterministic `classify_replay_policy`; LLM text is ignored
- classes: `SAFE_TO_REPLAY`, `MUST_REQUERY_EXTERNAL_STATE`, `VERIFY_BEFORE_REPLAY`, `NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION`
- default unknown behavior: strict `NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION`
- committed action: duplicate dispatch suppressed; external reads re-query under a new lineage action
- unresolved action: dispatch blocked until verification

## Unknown outcome

- supported: `true`
- verification_required: `true`; `UNKNOWN_OUTCOME -> VERIFY_REQUIRED`
- restart_behavior: persisted `RUNNING` is recovered to `UNKNOWN_OUTCOME -> VERIFY_REQUIRED`; no blind replay
- verification verdicts: `VERIFIED_EXISTS -> COMMITTED`, `VERIFIED_ABSENT -> FAILED`, `AMBIGUOUS -> VERIFY_REQUIRED`

## P1 checkpoint integration

- mission authority: P1 checkpoint remains sole owner of objective, phase, next action, and mission progression
- execution authority: action ledger owns only action status, replay class, lifecycle timestamps, and result/verification references
- projection: bounded action status is appended to the deterministic P1 projection on restore

## External authorities

- approval: reference-only; no ledger field grants approval
- safety: reference-only; existing guardrails remain in front of ledger execution
- financial: reference-only; strict replay class
- repo_router: remains external
- CodeGraph: remains external
- convergence: remains external

## Tests

- p1_passed: `35`
- p2_focused_passed: `39`
- focused combined passed: `74`
- canonical passed: `552` via runner plus `238` direct TUI tests = `790`
- canonical_failed_real: `0`
- blocked_infrastructure: `9` unchanged gateway async tests; missing `pytest-asyncio`, exact error `async def functions are not natively supported`
- TUI runner note: wrapper runner stalled on the unchanged TUI file in both P1 and P2 worktrees; direct pytest completed `238 passed`

## Adversarial

Passed:

- timeout/exception after dispatch becomes `UNKNOWN_OUTCOME`
- committed action cannot dispatch twice
- `RUNNING` restart recovery blocks replay
- changed input creates a new fingerprint and action identity
- rejected and superseded actions cannot replay
- unknown policy is strict and model text cannot override it
- external read re-query uses new action lineage
- authority references do not grant execution authority
- P1 restore injects action ledger state without changing checkpoint ownership
- session rotation preserves action identity

Failed: `0`

## Security

- secret_values_exposed: `0`
- raw tool output is not copied into action rows; result references are bounded

## Exactly-once distinction

- hermes_duplicate_replay_prevention: `true` for durable action identities known to Hermes
- provider_exactly_once_claimed: `false`
- scope: at-most-once dispatch protection plus verification-before-retry; external systems may not provide exactly-once semantics

## Invariants

```text
ACTION_ID_MACHINE_OWNED=true
ACTION_ID_SESSION_INDEPENDENT=true
ACTION_STATUS_MACHINE_OWNED=true
ACTION_LEDGER_DURABLE=true
ACTION_LEDGER_SURVIVES_RESTART=true
INPUT_FINGERPRINT_DETERMINISTIC=true
RUNNING_PERSISTED_BEFORE_DISPATCH=true
COMMITTED_PERSISTED_BEFORE_CONTINUATION=true
UNKNOWN_OUTCOME_SUPPORTED=true
UNKNOWN_OUTCOME_BLOCKS_REPLAY=true
VERIFY_REQUIRED_SUPPORTED=true
COMMITTED_ACTION_NOT_REEXECUTED=true
REPLAY_POLICY_DETERMINISTIC=true
UNKNOWN_SIDE_EFFECT_FAILS_CLOSED=true
CONVERSATION_CANNOT_OVERRIDE_ACTION_STATUS=true
MODEL_REPLACEMENT_CANNOT_OVERRIDE_ACTION_STATUS=true
MISSION_CHECKPOINT_REMAINS_MISSION_AUTHORITY=true
ACTION_LEDGER_REMAINS_EXECUTION_AUTHORITY=true
EXTERNAL_AUTHORITIES_REMAIN_AUTHORITATIVE=true
NO_UNIVERSAL_EXACTLY_ONCE_CLAIM=true
NON_DURABLE_BEHAVIOR_UNBROKEN=true
P1_INVARIANTS_PRESERVED=true
NO_LIVE_EXTERNAL_MUTATION=true
SECRET_VALUES_EXPOSED=0
PLAY_EXECUTED=false
```

P3 not started. No provider writes, campaign mutations, financial writes, deployments, approvals, or destructive external actions executed.

