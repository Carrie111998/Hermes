# Hermes Durable Action Commit P2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a SessionDB-backed action ledger that durably prevents duplicate Hermes tool execution and fails closed on uncertain outcomes while preserving P1 mission authority.

**Architecture:** `agent/action_commit.py` owns typed action identity, fingerprints, replay policy, and lifecycle validation. `SessionDB` stores action rows and performs atomic transitions. Existing policy/middleware gates remain unchanged; the ledger wraps the post-gate/pre-dispatch registry seam and the shared inline-tool execution helper. P1 checkpoints may reference action IDs but do not own action status.

**Tech Stack:** Python, SQLite, dataclasses, SHA-256 canonical JSON, pytest, existing Hermes tool registry/middleware.

**Spec:** `HERMES_DURABLE_ACTION_COMMIT_P2_IMPLEMENTATION_V1` in the mission prompt.

## Global Constraints

- No live provider calls or external mutations.
- No ActionCommitStore duplicate database or new MissionEngine.
- P1 checkpoint state remains the sole mission-progression authority.
- Action ledger owns execution status only.
- `RUNNING` persists before dispatch; terminal outcome persists before continuation.
- Uncertain post-dispatch results become `UNKNOWN_OUTCOME` and require verification.
- Unknown side-effecting tools fail closed.
- Existing approval, safety, financial, routing, CodeGraph, and convergence authorities remain external.
- Preserve ordinary non-durable tool behavior and all P1 invariants.

### Task 1: Add failing action-domain tests

**Files:**
- Create: `tests/agent/test_action_commit.py`

**Interfaces:**
- Tests will define the required public names: `ActionRecord`, `ActionStatus`, `ReplayClass`, `ActionLedgerError`, `canonical_input_fingerprint`, `classify_replay_policy`, and SessionDB action methods.

- [ ] Write focused tests for generated session-independent IDs, deterministic fingerprints, policy classes, valid/invalid transitions, and secret-safe metadata.
- [ ] Add tests for create/get/list/fingerprint lookup, reopen survival, and atomic state transition behavior.
- [ ] Run `scripts/run_tests.sh -j 1 tests/agent/test_action_commit.py -- --tb=short` and verify collection fails because the new API is absent.

### Task 2: Implement action domain and SessionDB storage

**Files:**
- Create: `agent/action_commit.py`
- Modify: `hermes_state.py`
- Test: `tests/agent/test_action_commit.py`

**Interfaces:**
- `ActionStatus` includes `PLANNED`, `AUTHORIZED`, `RUNNING`, `COMMITTED`, `FAILED`, `UNKNOWN_OUTCOME`, `VERIFY_REQUIRED`, `REJECTED`, and `SUPERSEDED`.
- `ReplayClass` includes `SAFE_TO_REPLAY`, `MUST_REQUERY_EXTERNAL_STATE`, `VERIFY_BEFORE_REPLAY`, and `NEVER_REPLAY_WITHOUT_NEW_AUTHORIZATION`.
- `canonical_input_fingerprint(tool_name: str, args: dict) -> str` returns a SHA-256 digest over stable tool name and normalized arguments.
- `classify_replay_policy(tool_name: str, args: dict | None = None) -> ReplayClass` is deterministic and defaults unknown side-effecting tools to strict rejection.
- `SessionDB.create_action(...)`, `get_action(action_id)`, `find_action_by_fingerprint(...)`, `list_pending_actions(mission_id, checkpoint_id=None)`, and transition methods persist typed rows.

- [ ] Add failing tests for additive migration and idempotent startup.
- [ ] Add `mission_actions` schema with explicit identity/status/fingerprint/timestamp columns and bounded JSON metadata.
- [ ] Implement typed record serialization/deserialization with strict bounds and no raw secret persistence.
- [ ] Implement atomic create and transition methods using `_execute_write`.
- [ ] Implement transition validation and reject illegal status changes.
- [ ] Run the focused tests until green.

### Task 3: Add execution protocol and registry integration

**Files:**
- Modify: `model_tools.py`
- Modify: `agent/agent_runtime_helpers.py`
- Modify: `agent/tool_executor.py` only if shared wiring requires it
- Test: `tests/agent/test_action_commit.py`

**Interfaces:**
- `prepare_action_for_dispatch(agent, tool_name, args, mission_id, checkpoint_id) -> ActionRecord` creates/reuses an action and returns a dispatch decision.
- `record_action_outcome(agent, action_id, outcome, ...)` persists `COMMITTED`, `FAILED`, or `UNKNOWN_OUTCOME` before returning to the conversation loop.
- Existing middleware, approval, safety, and guardrail calls run before ledger preparation.

- [ ] Add red tests proving `RUNNING` is persisted before a fake dispatch callback.
- [ ] Add red tests proving committed/rejected/superseded/unresolved actions never dispatch blindly.
- [ ] Wrap registry dispatch after existing gates and before `registry.dispatch`.
- [ ] Wrap inline agent-tool execution through the shared execution helper without changing policy decisions.
- [ ] Persist success/failure/uncertain outcomes before returning results.
- [ ] Ensure ledger write failures prevent dispatch or fail closed as `UNKNOWN_OUTCOME`.
- [ ] Run focused integration tests until green.

### Task 4: Add resume, verification, and P1 linkage

**Files:**
- Modify: `agent/action_commit.py`
- Modify: `agent/durable_mission.py`
- Modify: `hermes_state.py`
- Modify: `agent/turn_context.py`
- Test: `tests/agent/test_action_commit.py`

**Interfaces:**
- `resolve_resume_state(action) -> ResumeDecision` maps unresolved actions to `VERIFY_REQUIRED` without consulting conversation text.
- `verify_action_outcome(action_id, verdict) -> ActionRecord` accepts only deterministic `VERIFIED_EXISTS`, `VERIFIED_ABSENT`, or `AMBIGUOUS` verdicts.

- [ ] Add tests for restart discovery, model replacement, compression/session rotation, summary conflict, and checkpoint authority precedence.
- [ ] Implement deterministic verification transitions; ambiguous state remains blocked.
- [ ] Surface bounded action status/policy in durable mission projection without making it mission progression state.
- [ ] Preserve P1 non-durable and checkpoint tests.
- [ ] Run focused and P1 test files.

### Task 5: Adversarial and canonical certification

**Files:**
- Modify: `tests/agent/test_action_commit.py` only for missing adversarial cases.
- Create: `docs/receipts/HERMES_DURABLE_ACTION_COMMIT_P2_IMPLEMENTATION_V1.md`
- Modify: `docs/superpowers/plans/2026-08-19-hermes-durable-action-commit-p2.md`

- [ ] Run adversarial timeout, crash-before-response, crash-before-commit, changed fingerprint, committed replay, unknown-tool, and authority-reference tests.
- [ ] Run P1 focused tests and canonical SessionDB/tool/startup/compression regressions.
- [ ] Classify unchanged Gateway pytest-asyncio failures as blocked infrastructure if reproduced.
- [ ] Review diff, secret scan, compile/import smoke, and worktree cleanliness.
- [ ] Write the final receipt with exact SHA, test counts, invariants, and no universal exactly-once claim.
- [ ] Commit only intended P2 scope and stop; do not start P3.
