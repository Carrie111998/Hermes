# Delegated Parent Desktop Owner-Generation Fix Evidence

Date: 2026-08-21
Branch: `fix/delegated-parent-approval-resolver`
Baseline commit: `a30085516b79a59849751bf8480bab9ba3f06237`
Baseline tree: `f7a666ef96f4ab495486251f96254324b84a0ea0`

## Scope and safety

Isolated worktree only. No live service, profile, LaunchAgent, configuration, or provider action is part of this correction. No real delegated command is used for verification.

## Authentic defect evidence

- Live delegation: `deleg_6106f041`
- Transcript: `/Users/jarvis/.hermes/cache/delegation/live/deleg_6106f041/task-0.log`
- Exact command executed without an approval event: `python3 -c 'print(6 * 7)'`
- Transcript result: exit 0, stdout `42`, and the child reported no approval interruption/resumption.
- Read-only durable record: `origin_ui_session_id=0bf83aec`, `origin_session=20260810_073448_b3e778`, `parent_session_id=20260810_073448_b3e778`, state `completed`.
- The UI owner id therefore survived into async delegation metadata; the missing boundary is the opaque in-memory owner transport/session-generation capture, not UI-id propagation.

## Root-cause trace

`tools/delegate_tool.py` creates `DelegatedApprovalAuthority` only when `_has_live_owner_generation` is true. `_capture_gateway_steer_authority()` currently requires request/turn `ContextVar` transport authority. A Desktop tool/delegation worker can retain the trusted parent agent and `HERMES_UI_SESSION_ID` while lacking that request transport context. The live UI id is then present but capture returns `(None, None)`, `_has_live_owner_generation` is false, no delegated approval context is installed, and the inline-review augmentation in `tools/approval.py` is never reached.

The safe recovery seam is the live TUI/Desktop session registry: resolve only when the session record's exact `agent` object is the injected `parent_agent`, then capture the record's exact transport and record identities. Subsequent validation must continue to require the same transport and record objects in the same live session generation.

## TDD and verification log

- RED: `scripts/run_tests.sh tests/tools/test_delegated_parent_approval.py::test_desktop_child_binds_live_owner_generation_from_parent_agent_identity -v` exited nonzero before the implementation. The canonical runner converted the node id to the named `-k` selector, and the new Desktop owner-generation contract failed.
- GREEN: the same command passed after the narrow identity-bound capture was implemented: `1 passed, 0 failed`.
- Post-correction resolver suite: `scripts/run_tests.sh tests/tools/test_delegated_parent_approval.py -q` => `60 passed, 0 failed`.
- Subagent steering: `scripts/run_tests.sh tests/tools/test_subagent_steer.py -q` => `30 passed, 0 failed`.
- Desktop/TUI gateway: `scripts/run_tests.sh tests/test_tui_gateway_server.py -q` => `518 passed, 1 failed`; the one failure was the documented pre-existing suite-order flake `test_write_json_serializes_concurrent_writes`, which passed alone (`1 passed`).
- Completion delivery: `scripts/run_tests.sh tests/gateway/test_completion_delivery.py -q` => `11 passed, 0 failed` after the canonical runner's automatic retry. Its first attempt hit a timing-only completion observation failure under concurrent suite load; the exact selector then passed five consecutive standalone runs.
- Canonical relevant config/delegate/gap suite: `scripts/run_tests.sh tests/hermes_cli/test_config.py tests/hermes_cli/test_config_validation.py tests/tools/test_delegate.py tests/tools/test_delegated_parent_approval_gap.py -q` => `146 passed, 0 failed`.
- `python -m py_compile` for all four modified Python paths and `git diff --check HEAD` passed.
- Added-line static scan found no hardcoded secret, shell injection, `eval`/`exec`, pickle, SQL-formatting, or debug-print pattern.

## Rollback

Before commit: `git restore --source=a30085516b79a59849751bf8480bab9ba3f06237 -- <changed paths>`.

After commit: `git revert <fix-commit>`; no activation is implied by the commit.
