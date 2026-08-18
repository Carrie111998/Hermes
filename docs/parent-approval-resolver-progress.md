# Parent delegated-command approval resolver — progress and freeze evidence

## Authority and state

- Worktree: `/Users/jarvis/.hermes/worktrees/forge-parent-approval-resolver`
- Branch: `fix/delegated-parent-approval-resolver`
- Baseline commit: `2dca286ff22b56e14faf5b267eebdd24bd6d604c`
- Baseline tree: `767beffa6d60fb894b36d89e41c1f503161ac7ea`
- Baseline parent: `4924696832da71094f7346428a08590db6e92db2`
- Baseline subject: `docs: design delegated parent approval resolver`
- Authorized closeout: source/tests/docs/evidence and one local commit only.
- Explicitly excluded: live install, live config change, service/session restart, activation, push, PR, merge, or deployment.
- Feature state: implemented but `approvals.delegated_parent.enabled: false` by default; experimental/non-live pending independent acceptance and a separate operator activation/restart decision.

No current implementation bytes were reset or rewritten from baseline. Recovery continued from the retained dirty worktree after two timed-out child runs.

## Evidence provenance

Complete append-only transcripts read for recovery and RED→GREEN history:

1. `/Users/jarvis/.hermes/cache/delegation/live/deleg_9c1720fa/task-0.log`
2. `/Users/jarvis/.hermes/cache/delegation/live/deleg_330330b4/task-0.log`
3. `/Users/jarvis/.hermes/cache/delegation/live/deleg_f3daf06b/task-0.log`

The first two timed out after preserving implementation/test bytes. The third timed out after completing the dynamic-command, delivery, schema-hiding, and lifecycle slices. Later bounded verification recovered the same worktree; no failed run was treated as green.

## Authentic RED→GREEN boundaries

### Transcript 1 — authority and exact-once core (`deleg_9c1720fa`)

- RED: `HERMES_PYTHON=/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python scripts/run_tests.sh tests/tools/test_delegated_parent_approval.py -q` failed during collection because the new `tools.delegated_approval` contract did not exist.
- GREEN after the context authority module and ContextVar binding were introduced: the same command passed the first authority-context contract.
- RED after exact-once request tests were added: the focused file failed because the initial stub did not register/wait/resolve a request.
- GREEN after the in-memory request registry, object-identity checks, digest binding, expiry, and event wake were implemented: the same focused command passed the then-current exact-once slice.
- The transcript then wired config capture, active-child authority, guard interception, resolver schema/handler, gateway/TUI event routing, and revocation, but timed out before integrated acceptance. Those bytes were preserved for the next run.

### Transcript 2 — recovered integration and lifecycle (`deleg_330330b4`)

- RED: `/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/tools/test_delegated_parent_approval.py -q` exposed classifier and request-keying failures after the contract expanded.
- GREEN: the same file reached `11 passed in 0.40s` after replacing heuristic consequence claims with the exact structured interpreter class and fixing per-subagent request selection.
- RED: two exact selectors for expiry/transport replacement failed because the wait did not observe live authority replacement and a reused helper could select the wrong pending request.
- GREEN: polling now validates the active transport/session generation and revokes on replacement; the focused file returned `11 passed in 0.44s`.
- Integrated affected suite: `tests/tools/test_delegate.py tests/tools/test_approval.py tests/tools/test_async_delegation.py tests/tools/test_process_registry.py tests/tools/test_delegated_parent_approval.py` later qualified as 260 passed on the recovered bytes.
- Run-agent/steering suite later qualified as 296 passed, 1 skipped.
- The full TUI run exposed the known suite-order-only `test_write_json_serializes_concurrent_writes` flake. Its exact isolated selector passed; no production change was made for that unrelated pre-existing behavior.

### Transcript 3 — dynamic command and fresh-turn delivery (`deleg_f3daf06b`)

- RED: `/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/tools/test_delegated_parent_approval.py::test_dynamic_command_never_prelisted_reaches_parent_once_and_resumes tests/tools/test_delegated_parent_approval.py::test_lifecycle_revocation_unblocks_child_as_deny -q` returned `2 failed`; runtime still depended on static digest pre-attestation and lacked complete process-exit revocation.
- GREEN: after removing static digest prelisting and adding process-exit revocation, the same command returned `2 passed in 0.28s`.
- RED: the expanded focused classifier file returned one failure because a test mislabeled `python -m pytest` as the interpreter `-e`/`-c` class. The inaccurate assertion was removed; no runtime widening was made.
- GREEN: the focused file returned `23 passed in 0.68s`, then `27 passed in 0.93s` after lifecycle coverage was added.
- RED→GREEN TUI delivery: `tests/test_tui_gateway_server.py::test_delegated_approval_event_runs_as_fresh_typed_turn_without_approval_card` first failed at the real prompt-turn seam, then passed (`1 passed in 0.58s`) after the typed timeline/turn test used the production call contract.
- Gateway delivery: `tests/gateway/test_completion_delivery.py::test_delegated_approval_event_uses_gateway_fresh_turn_delivery` passed (`1 passed in 0.17s`).
- RED→GREEN child schema: `tests/tools/test_delegated_parent_approval.py::test_child_schema_never_advertises_parent_resolver_operation` first failed because no child-schema stripping seam existed, then passed (`1 passed in 0.16s`) after pre-first-call deep-copy stripping was implemented.

## Implemented contract

- Trusted parent-side config capture; exact YAML boolean `true` required, default false.
- Active top-level background child only; forced synchronous fallback disables the lane.
- Non-serializable `DelegatedApprovalAuthority` bound to one child execution.
- Handler-injected parent object + active child/registry authority; Desktop/TUI transport and session-record generation identity when applicable.
- Dynamic request created only after the exact child command exists; SHA-256 digest, tool-call id, request id, child, and authority are bound in memory.
- Eligibility is only local/no-host, no Tirith findings, non-empty <=8192-byte command, and the sole structured inline interpreter `-e`/`-c` pattern class (canonical key or exact compatibility alias).
- `once`, `deny`, and `escalate_to_user` only; no session/always/config mutation.
- Child/sibling/unrelated/self/replay/substitution denied generically.
- Child schema hides the resolver before its first model call; parent schema and registry schema are not mutated.
- TUI/gateway deliver a bounded redacted system-authored event as a fresh turn; no user approval card for the parent decision turn.
- Raw command remains in the in-memory blocking entry. Event and audit payloads carry bounded secret-redacted display plus digest/ids.
- Completion, interrupt, parent reset, transport/session replacement, expiry, and process exit revoke pending entries.
- Hardline/ineligible behavior stays on the existing unconditional-block or user-approval path.

## Exact intended file inventory

Modified from baseline:

1. `agent/delegation_context.py`
2. `docs/delegated-parent-approval-resolver.md`
3. `gateway/run.py`
4. `hermes_cli/config_defaults.py`
5. `run_agent.py`
6. `tests/gateway/test_completion_delivery.py`
7. `tests/hermes_cli/test_config.py`
8. `tests/test_tui_gateway_server.py`
9. `tools/approval.py`
10. `tools/delegate_tool.py`
11. `tools/process_registry.py`
12. `tui_gateway/server.py`
13. `website/docs/user-guide/configuration.md`
14. `website/docs/user-guide/security.md`

New:

15. `docs/parent-approval-resolver-progress.md`
16. `tests/tools/test_delegated_parent_approval.py`
17. `tools/delegated_approval.py`

No other path is intended for the commit.

## Verification receipts

### Broad recovered-tree qualification already completed

- Resolver/approval/delegate/async/process: 260 passed.
- Run-agent/steering/subagent-steer: 296 passed, 1 skipped.
- Gateway completion plus full TUI: 526 passed.
- Focused resolver contract: 27 passed.
- `python3 -m py_compile` over all changed Python source: passed.
- `git diff --check`: passed.

These are retained qualification totals from the same dirty candidate after the third timeout. The closeout did not rerun huge suites unnecessarily.

### Final bounded closeout commands

Config/default/validation coverage was located under `tests/hermes_cli/`; there is no global closed-world schema file for nested `approvals` keys. A loader assertion was added to the existing `TestLoadConfigDefaults` contract.

```text
/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/hermes_cli/test_config.py::TestLoadConfigDefaults tests/hermes_cli/test_config_validation.py -q
=> 10 passed in 0.18s

/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/tools/test_delegated_parent_approval.py -q
=> 27 passed in 1.14s

/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/gateway/test_completion_delivery.py::test_delegated_approval_event_uses_gateway_fresh_turn_delivery tests/test_tui_gateway_server.py::test_delegated_approval_event_runs_as_fresh_typed_turn_without_approval_card -q
=> 2 passed in 0.44s

/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python -m pytest tests/test_tui_gateway_server.py::test_write_json_serializes_concurrent_writes -q
=> 1 passed in 0.48s

python3 -m py_compile agent/delegation_context.py gateway/run.py hermes_cli/config_defaults.py run_agent.py tools/approval.py tools/delegate_tool.py tools/process_registry.py tui_gateway/server.py tools/delegated_approval.py tests/tools/test_delegated_parent_approval.py tests/gateway/test_completion_delivery.py tests/test_tui_gateway_server.py tests/hermes_cli/test_config.py
=> PASS

git diff --check HEAD
=> PASS
```

### Canonical hermetic runner correction

A post-commit canonical-runner check found that the new resolver test module passed under the delegated session environment but failed four assertions under `scripts/run_tests.sh`'s `env -i` isolation. Root cause: importing `tools.approval` at collection loaded the operator's default-profile permanent/session approval globals before the test fixture redirected runtime state; the existing delegated session environment happened not to contain those approvals and masked the test dependency. Runtime behavior was not changed. The resolver test fixture now snapshots, clears, and restores `_permanent_approved`, `_session_approved`, and `_session_yolo` under the approval lock.

```text
HERMES_PYTHON=/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python scripts/run_tests.sh tests/tools/test_delegated_parent_approval.py -q
=> 27 passed, 0 failed

HERMES_PYTHON=/Users/jarvis/.hermes/worktrees/forge-test-baseline-phase2/.venv/bin/python scripts/run_tests.sh tests/hermes_cli/test_config.py tests/tools/test_delegated_parent_approval.py tests/tools/test_delegate.py -q
=> 164 passed, 0 failed
```

`git diff --check HEAD` and `py_compile` for the three directly relevant Python paths also passed after the correction.

Repository-wide stale-symbol search found `allowed_command_digests` and `eligible_command_digests` only in the negative config assertions and this audit note. No runtime/config/design static digest list or contradictory claim remains.

## Known limitation and acceptance gates

- No live Hermes install, config, gateway, Desktop/TUI service, or real model session was changed or exercised. Verification is source-level and test-harness based.
- The feature intentionally handles only the narrow structured interpreter `-e`/`-c` false-positive class. It is not a general parent authorization system.
- Pending capabilities are process-local and are not restart-resumable; restart/reset fails closed.
- Parent event text is redacted and bounded, so the digest is authoritative if display text is truncated or redacted.
- The known `test_write_json_serializes_concurrent_writes` suite-order flake is pre-existing and unrelated; its exact selector passes alone.
- Commit acceptance is not activation authority. Quinn (or another independent security reviewer) must review the exact commit/tree, object-identity authority, eligibility boundary, redaction, schema hiding, and lifecycle revocation.
- After independent acceptance, Rich must make a separate decision for any live config edit and required service/session restart. Push/PR/merge/deploy remain separate gates.

## Rollback

Pre-activation: revert the feature commit. The default stays false, pending state is memory-only, and there is no static allowlist or config migration to remove.

Post-activation (only under a future explicit gate): first return `approvals.delegated_parent.enabled` to `false`; then perform only the separately authorized service/session restart needed to load the change. Pending entries fail closed on reset/restart/exit. Revert the implementation commit if code rollback is also required.

## No-live-change assertion

This worktree closeout did not install code, edit any active profile config, start/stop/restart a service, activate the feature, contact an external system, push a branch, or open a PR. The only authorized terminal mutation after qualification is staging the exact inventory above and creating the local commit named by the task.
