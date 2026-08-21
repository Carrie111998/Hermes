# Delegated Parent Desktop Async Resolver Successor Evidence

Date: 2026-08-21
Branch: `fix/delegated-parent-approval-resolver`
Original security baseline: `a30085516b79a59849751bf8480bab9ba3f06237`
Original baseline tree: `f7a666ef96f4ab495486251f96254324b84a0ea0`
Rejected owner-generation candidate / successor parent: `f70b453274d7d5f37aa6176701e7546841eae543`
Successor-parent tree: `0c9c780f8ae3965a71e9ec30355436be8d4ec5cc`

## Scope and safety

This correction was finalized only in the isolated worktree. No live service, profile, LaunchAgent, configuration, provider, credential, network, push, merge, or activation action was performed. The integration test uses a non-effectful fake executor; it does not execute the command under review.

## Authentic defect evidence

- Live delegation: `deleg_6106f041`.
- Transcript: `/Users/jarvis/.hermes/cache/delegation/live/deleg_6106f041/task-0.log`.
- Exact command that previously executed without an approval event: `python3 -c 'print(6 * 7)'`.
- Transcript result: exit 0, stdout `42`, and no child approval interruption/resumption.
- Read-only durable record: `origin_ui_session_id=0bf83aec`, `origin_session=20260810_073448_b3e778`, `parent_session_id=20260810_073448_b3e778`, state `completed`.
- The UI owner id survived into async delegation metadata. The first missing boundary was the opaque in-memory owner transport/session-generation capture, not UI-id propagation.

## Two-part root cause and minimal correction

1. `tools/delegate_tool.py` creates `DelegatedApprovalAuthority` only when a live owner generation is captured. Desktop async workers can retain the trusted parent-agent object and `HERMES_UI_SESSION_ID` after request transport context is intentionally shed. The predecessor capture path required request/turn transport authority, so the owner-generation candidate `f70b453` added an identity-bound fallback through the live Desktop/TUI session record. It accepts only the record whose exact `agent` object is the injected parent agent and retains the exact transport and record objects for later generation checks.
2. Authentic dispatch then exposed a second root cause in `tools/approval.py`: the legacy noninteractive fast return occurred before delegated inline-review augmentation and the existing parent lane. Even a valid live `DelegatedApprovalAuthority` therefore auto-approved without emitting an event. The successor lets only an actual, enabled `DelegatedApprovalAuthority` bypass that one early return. Serialized dictionaries, lookalike objects, disabled authorities, and authority-lookup exceptions cannot cross the `isinstance`/enabled-context boundary. All existing parent-lane eligibility, request identity, owner identity, session-record identity, transport identity, decision, revocation, and timeout checks remain unchanged and fail closed.

## Authentic RED/GREEN proof

- RED on unchanged `f70b453`: the new suite drove actual `delegate_tool._handle_delegate_task()` async dispatch, deliberately removed request transport, entered the real background child worker, and called the real command guard. The exact-owner test failed because no `delegated_approval_request` event was emitted; the fake executor observed the legacy silent approval path.
- GREEN after the minimal `tools/approval.py` correction: the same exact-owner flow emitted the full approval event, rejected a same-session-id impostor parent, accepted the exact owner once, resumed the same child once on the same worker thread, invoked the fake executor exactly once, and rejected a duplicate decision as unavailable.
- Fail-closed cases: no decision timed out with zero execution; replacing the live Desktop session record, transport, or agent made resolution unavailable with zero execution. Added adversarial coverage proves serialized, lookalike, disabled, and exception-producing authority contexts cannot bypass legacy noninteractive behavior.

## Verification log

Completed before final successor commit:

- New authentic Desktop async integration suite: `scripts/run_tests.sh tests/tools/test_delegated_parent_approval_desktop_async.py -q` => `9 passed, 0 failed` (five async dispatch cases plus four authority-boundary parameter cases).
- Resolver suite: `scripts/run_tests.sh tests/tools/test_delegated_parent_approval.py -q` => `60 passed, 0 failed`.
- Subagent steering: `scripts/run_tests.sh tests/tools/test_subagent_steer.py -q` => `30 passed, 0 failed`.
- Async delegation plus delegate suite: `scripts/run_tests.sh tests/tools/test_async_delegation.py tests/tools/test_delegate.py -q` => `83 passed, 0 failed`.
- Completion delivery: `scripts/run_tests.sh tests/gateway/test_completion_delivery.py -q` => `11 passed, 0 failed`.
- Config/config-validation/gap suite: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/hermes_cli/test_config_validation.py tests/tools/test_delegated_parent_approval_gap.py -q` => `73 passed, 0 failed`.
- Desktop/TUI full file on the candidate: `518 passed, 1 failed`.
- The same Desktop/TUI full-file result reproduced on detached parent baseline `a30085516b79a59849751bf8480bab9ba3f06237`: `518 passed, 1 failed`, with the same pre-existing suite-order flake `test_write_json_serializes_concurrent_writes`.
- That flaky node passed five consecutive standalone runs; the related integration selector also passed five consecutive standalone runs. No bytes affecting the TUI implementation changed after that comparison, so the 500-test file was not rerun.
- Final noninteractive and authentic async authority checks: `scripts/run_tests.sh tests/tools/test_delegated_parent_approval_desktop_async.py -q` => `9 passed, 0 failed`.
- Final cron guard checks: `scripts/run_tests.sh tests/tools/test_cron_approval_mode.py -q` => `30 passed, 0 failed`.
- Final approval/gateway guard checks: `scripts/run_tests.sh tests/tools/test_approval.py -q` => `96 passed, 0 failed`.
- `.venv/bin/python -m py_compile tools/approval.py tests/tools/test_delegated_parent_approval_desktop_async.py` and `git diff --check` passed.
- The final staged added-line scan found no hardcoded-secret assignment, shell injection, dangerous `eval`/`exec`, unsafe pickle deserialization, SQL-formatting injection, or debug-print pattern.

## Rollback

Before successor commit: restore tracked paths from `f70b453274d7d5f37aa6176701e7546841eae543` and remove only `tests/tools/test_delegated_parent_approval_desktop_async.py`.

After successor commit: `git revert <successor-commit>`. Reverting or committing does not activate, deploy, push, merge, or restart anything.
