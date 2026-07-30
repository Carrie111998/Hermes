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
- keep the existing one-shot destructive confirmation primitive intact;
- send real progress through the platform after at most about 90 seconds;
- terminate every run as done, blocked, or failed and distinguish a generated
  final from a delivered, unknown, or failed final;
- make an owner stop durable and permanently remove hygiene dependency edges
  that can implicitly start the gateway.

The database-native route client and any Maione-specific effect taxonomy are
not part of this Hermes-core change. Existing canonical and domain skills
continue to own business intent, target resolution, and protected-effect
confirmation.

## Baseline and isolation

- repository: `NousResearch/hermes-agent`
- branch: `codex/hermes-direct-ops-safety`
- clean origin/main baseline, local HEAD, and merge base before edits:
  `8defb9fd60bebe2802eaab7c57fa2ee6a4ff6281`
- implementation worktree:
  `C:\Users\Ed\.codex\worktrees\hermes-direct-ops-0730`
- no service, systemd, runtime, deploy, unmask, start, restart, provider, or
  business-data mutation command was executed from this worktree

## Final no-retry behavioral gate

The canonical runner was used with all 23 changed-area test files in one
invocation, `--file-timeout 900`, and `--file-retries 0`.

Result:

```text
23 files, 566 tests passed, 0 failed
runner wall: 186.8 seconds
process exit: 0
```

The immutable Base64 evidence files below decode byte-for-byte to the captured
stdout, stderr, and exit-code files:

| Evidence | Decoded bytes | Decoded SHA-256 | Base64 artifact SHA-256 |
|---|---:|---|---|
| `docs/security/evidence/2026-07-30-hermes-direct-ops-focused.stdout.log.b64` | 4471 | `6eb4f010b60cd3b854062c7cd50548d28c364b51ea724a2e20d4c4ed1391c426` | `0236456e1b666e953b27a9a5246d46c02d88c8d841fe851201f7c0e463510464` |
| `docs/security/evidence/2026-07-30-hermes-direct-ops-focused.stderr.log.b64` | 561 | `3c89fe112ead23f54f7fe916bf7c72ca81450b69a92a875dc781daf8d787a852` | `5da270a65920a1c239dea57d807a40176f419692081885c014f7b3f1d38f5d72` |
| `docs/security/evidence/2026-07-30-hermes-direct-ops-focused.exit.txt.b64` | 3 | `13bf7b3039c63bf5a50491fa3cfd8eb4e699d1ba1436315aef9cbe5711530354` | `3cb85ee278ce6ab31f25c4bbb991a6218334a4b06c26ca45f983641f10ce1452` |

The stderr artifact contains only the known WSL worktree gitdir-pointer
diagnostic plus PowerShell progress serialization. The runner summary and
captured process exit are green.

Supporting preservation gate for the existing one-shot destructive
confirmation primitive:

```text
tests/gateway/test_destructive_slash_confirm.py
tests/hermes_cli/test_destructive_slash_confirm_gate.py
tests/tools/test_slash_confirm.py
3 files, 14 tests passed, 0 failed, retries disabled
```

High-signal regressions in the 566-test gate include:

- `test_phase_classifier_preserves_business_operations`
- `test_business_api_operation_remains_allowed_even_when_repo_is_dirty`
- `test_exact_quote_investigation_declines_codex_exec_and_patch`
- `test_dirty_implementation_is_automatically_isolated`
- `test_clean_implementation_codex_thread_keeps_native_permissions`
- `test_actual_mcp_subprocess_cannot_escape_phase_with_terminal`
- `test_required_child_fails_closed_for_non_skill_effect_without_channel`
- `test_july_quote_failure_cannot_load_103k_and_119k_skill_packets`
- `test_ninety_second_progress_receipt_reaches_real_send_seam`
- `test_unknown_stream_outcome_suppresses_resend_without_claiming_delivery`
- `test_stream_ledger_failure_has_explicit_terminal_delivery_state`
- `test_unit_migration_removes_only_implicit_gateway_start_edges`
- `test_hardened_watchdog_exits_and_cannot_schedule_when_owner_hold_exists`
- `test_implicit_gateway_run_is_refused_while_owner_hold_exists`

## Static gates

- Python bytecode compilation: PASS
- `git diff --check`: PASS
- repository-wide blocking `ruff check .`: PASS
- `python scripts/check-windows-footguns.py --all`: PASS, 895 files scanned
- exact changed-file type inspection: completed; four new advisory diagnostics
  were removed before the final behavioral run

The repository pins an advisory `ty` release and CI invokes it with
`--exit-zero`. A directory-wide local invocation of `ty 0.0.21` panics on the
unchanged `tools/checkpoint_manager.py` cycle and also reports the repository's
large existing diagnostic baseline. This is not represented as a type-check
pass. The normal CI base-versus-head advisory report remains required during
integration.

The full repository test matrix and production packaging were not run in this
lane. They remain integration/release gates; the 566-test result is the complete
changed-area behavioral gate, not a claim about the full suite.

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

1. Review the three commits and run the full GitHub test, blocking lint,
   Windows-footgun, advisory type-diff, and packaging gates.
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
