# Hermes Direct Operations source gate — 2026-07-30

## Result

**READY FOR REVIEW. Scoped source gate: PASS. Live release: NOT PERFORMED.**

This evidence covers the minimal Hermes repair requested after the July 30
incidents:

- load only the smallest directly governing specialist skill set;
- keep investigations read-only and require explicit implementation authority
  for source or installed-skill changes;
- preserve ordinary business-operation execution and native agent tool use;
- isolate every Codex implementation turn in a clean dedicated worktree;
- treat external MCP effect annotations as untrusted and disable direct
  app/plugin/external-MCP surfaces outside operation turns;
- keep the existing one-shot destructive confirmation primitive intact;
- send real progress through the platform after at most about 90 seconds;
- terminate every run as done, blocked, failed, or cancelled and distinguish a
  generated final from a delivered, unknown, or failed final;
- make an owner stop durable and permanently remove hygiene dependency edges
  that can implicitly start the gateway.

The database-native route client and any Maione-specific effect taxonomy are
not part of this Hermes-core change. Existing canonical and domain skills
continue to own business intent, target resolution, and protected-effect
confirmation.

## Baseline and isolation

- repository: `NousResearch/hermes-agent`
- branch: `codex/hermes-direct-ops-safety`
- current origin/main and merge base after the final rebase:
  `cc4cab2f592e60a197e796506de9168f74baf3ea`
- rebased safety implementation commit before this evidence-only refresh:
  `46c1eac12fbc9d314c8e1127b1e86eeb6c98a221`
- implementation worktree:
  `C:\Users\Ed\.codex\worktrees\hermes-direct-ops-0730`
- no service, systemd, runtime, deploy, unmask, start, restart, provider, or
  business-data mutation command was executed from this worktree

## Final no-retry behavioral gate

The canonical runner was used after the final rebase with all 24
branch-touched behavior files plus the three existing one-confirmation files
in one invocation, `--file-timeout 900`, and `--file-retries 0`.

Result:

```text
27 files, 602 tests passed, 0 failed
runner wall: 186.5 seconds
process exit: 0
```

The runner emitted the known post-summary WSL/Windows worktree gitdir-pointer
diagnostic after reporting the green result; the process still exited zero.
The existing Base64 files under `docs/security/evidence/` preserve the earlier
pre-rebase run and are intentionally not represented as evidence for this
current result.

Supporting preservation gate for the existing one-shot destructive
confirmation primitive:

```text
tests/gateway/test_destructive_slash_confirm.py
tests/hermes_cli/test_destructive_slash_confirm_gate.py
tests/tools/test_slash_confirm.py
14 tests passed inside the 602-test gate, retries disabled
```

High-signal regressions in the 602-test gate include:

- `test_phase_classifier_preserves_business_operations`
- `test_business_api_operation_remains_allowed_even_when_repo_is_dirty`
- `test_exact_quote_investigation_declines_codex_exec_and_patch`
- `test_dirty_implementation_is_automatically_isolated`
- `test_clean_implementation_codex_thread_keeps_native_permissions`
- `test_operation_codex_thread_keeps_native_external_tools`
- `test_actual_mcp_subprocess_cannot_escape_phase_with_terminal`
- `test_required_child_fails_closed_for_non_skill_effect_without_channel`
- `test_external_tool_cannot_self_declare_read_only_during_investigation`
- `test_july_quote_failure_cannot_load_oversized_skill_packets`
- `test_failed_partial_native_file_effect_blocks_every_later_mutation`
- `test_ninety_second_progress_receipt_reaches_real_send_seam`
- `test_unknown_stream_outcome_suppresses_resend_without_claiming_delivery`
- `test_stream_ledger_failure_has_explicit_terminal_delivery_state`
- `test_cancelled_exception_closes_run_as_cancelled_receipt`
- `test_unit_migration_removes_only_implicit_gateway_start_edges`
- `test_systemd_stop_reaches_gateway_when_every_hygiene_stop_raises`
- `test_failed_systemd_start_restores_prior_owner_hold`
- `test_failed_systemd_restart_restores_prior_owner_hold`
- `test_hardened_watchdog_exits_and_cannot_schedule_when_owner_hold_exists`
- `test_implicit_gateway_run_is_refused_while_owner_hold_exists`

## Static gates

- Python bytecode compilation: PASS
- `git diff --check`: PASS
- repository-wide blocking `ruff check .`: PASS (one pre-existing invalid
  `noqa` warning, zero blocking diagnostics)
- `python scripts/check-windows-footguns.py --all`: PASS, 896 files scanned

The repository pins an advisory `ty` release and CI invokes it with
`--exit-zero`. A directory-wide local invocation of `ty 0.0.21` panics on the
unchanged `tools/checkpoint_manager.py` cycle and also reports the repository's
large existing diagnostic baseline. No fresh full-repository type-pass is
claimed here. The normal CI base-versus-head advisory report remains required
during integration.

The full repository test matrix and production packaging were not run in this
lane. They remain integration/release gates; the 602-test result is the complete
changed-area behavioral gate, not a claim about the full suite.

## Exact residual repository race

Hermes captures repository state at turn intake, re-probes immediately before
each native mutating tool, verifies typed file effects after execution, advances
its expected state only for a successful verified effect, and freezes later
mutations after an exception or partial/unknown effect. This closes the
observed dirty-checkout and partial-tool races as far as Hermes can enforce
inside its own process.

One narrow cross-process race remains: another program can modify a repository
after Hermes' final pre-write probe or source readback because Git and ordinary
filesystem writes do not provide a mandatory lock shared by every editor and
process. Hermes will detect that drift before its next mutation, but a final
response could theoretically race a later outside edit. Eliminating that last
window requires OS/container isolation or universal cooperative locking; it is
not silently represented as solved.

## Dependency and owner-stop proof

`harden_hygiene_unit_definition` removes the gateway from `Wants=`,
`Requires=`, `BindsTo=`, and `Upholds=` while preserving ordering-only
`After=` and unrelated dependencies. The migration precomputes both unit and
watchdog changes, backs up originals, verifies readback, compensates only when
the protected state still matches, and reloads the restored unit if activation
fails.

No live unit was edited by this lane. The release owner must re-read the
installed hygiene unit and confirm it contains no pull dependency on
`hermes-gateway.service` before any hold is removed.

## Controlled install and activation order

1. Review the four safety commits plus this evidence refresh, then run the full
   GitHub test, blocking lint, Windows-footgun, advisory type-diff, and
   packaging gates.
2. Install the reviewed source while the existing gateway/hygiene owner holds
   remain in place. Do not start Hermes as part of installation.
3. Run the hygiene migration explicitly and read back the installed service and
   watchdog sources. Confirm no `Wants=`, `Requires=`, `BindsTo=`, or
   `Upholds=` edge names `hermes-gateway.service`.
4. Verify the gateway, hygiene service, and hygiene timer remain inactive and
   the owner hold remains active. Perform read-only configuration and policy
   checks.
5. Only after explicit release-owner coordination, use the explicit start path;
   that path migrates hygiene before clearing the durable hold. Verify one
   harmless read-only run reaches a delivered terminal state and emits progress
   if deliberately held beyond 90 seconds.
6. Do not manufacture business data. A later genuine owner-requested operation
   is the only valid business canary and must use its existing specialist skill
   and source readback.

Automatic cleanup of detached implementation worktrees is deliberately
deferred: deleting an uncommitted isolated lane would be unsafe. A separate
owner-visible handoff/archive policy is follow-up operational debt.
