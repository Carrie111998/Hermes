# AgentOps Phase 2 G2 Remediation Evidence (Review Request)

**Branch:** `codex/agentops-phase-2-observer`
**Remediation base:** `1c03c2db6f5e050b80e6526b70b2e9b05c64805e`
**Authorization:** Phase 2 observer code in an isolated worktree only. This
does not authorize a production rollout, lifecycle operation, Gateway,
LaunchAgent or Cron change, LLM, Dashboard, R1-R4, merge or push.

Sol's reviews returned `changes_requested`; this document records the final
release-blocker remediation commit and its reproducible verification. It is
not a G2 approval claim; independent Sol review remains required.

## Scope and protected assets

- Phase 0/1 source and tests remain unchanged. This remediation changes only
  Phase 2 plugin-library files, its task plan/evidence, manifest and tests.
- No plugin hook, daemon route, Gateway registration, service management or
  scheduled job was added. The Bridge remains opt-in and unregistered.
- Target SQLite DB/WAL/SHM are never opened through SQLite. Their collector
  performs only `lstat` metadata reads and reports `integrity: unknown`.
- The only database opened read-write is fixed, preflighted,
  AgentOps-owned `observer.db` below the Phase 1 marker directory.

## Remediation evidence matrix

| Review finding | Remediation | Negative/regression evidence |
|---|---|---|
| P0-1 target SQLite side effect | Removed target SQLite API use entirely; collector supplies regular-file metadata only and explicit unknown integrity | Live writer retains DB/WAL/SHM hashes in `test_live_wal_target_files_are_never_opened_or_changed`; static source test rejects `sqlite3`/integrity call |
| P0-2 JSON/quoted password bypass | JSON log records parse then recursively redact; quoted assignment and value patterns re-scan at collector/store boundaries | `test_json_and_quoted_password_canaries_do_not_cross_log_store_boundary` verifies password/token/cookie absent from signals and `observer.db*` |
| P1-1 log loss/multi-file cursor collision | Source fingerprint is separate from Signal identity; source-aware cursor table keys `(target, collector, source)`; cursor moves only consumed complete/bounded bytes | `test_log_cursor_only_advances_consumed_lines_and_source_cursors_do_not_collide`, plus rotation/truncation tests |
| P1-2 Cron false-green/evidence loss | Missing/stale/failed assertion states are unhealthy; `collection_runs`, signal occurrences and run links persist health/reason/time/recurrence | `test_cron_missing_and_stale_assertions_are_unhealthy_and_runs_record_recurrence` and JSON source preservation test |
| P1-3 Bridge mutation/race | Queue contains canonical JSON, validates again before enqueue/drain, and holds a lock around queue/capacity state | `test_bridge_copies_nested_payload_revalidates_and_remains_capacity_bounded` |
| P1-4 Git traversal/layout | Strict ref grammar, root containment and no symlink components; direct read support for gitdir/commondir/packed-refs | `test_git_ref_traversal_is_rejected_and_standard_worktree_packed_refs_are_read` |
| P1-5 source binding/deadline/budgets | Target observed paths/labels bind sources; bounded source reads/items/rates; fan-out has caller-visible deadline | `test_asset_binding_deadline_and_snapshot_deep_freeze`, `test_process_plist_and_cron_collectors_enforce_item_or_byte_budgets` |
| P1-6 ObserverStore preflight | Marker/owner/mode/fixed-path/inode checks and read-only exact schema/version/integrity/constraint preflight precede writable SQLite/WAL | `test_unrelated_existing_observer_database_is_unchanged_before_preflight`, `test_same_named_incompatible_observer_schema_is_rejected_without_wal_or_bytes_change` |
| P1-7 manifest semantics | Versioned pack declares target kinds, bounded probes, assertions, classification, retention, no-write production-read/dry-run and manual failure runbook | `test_review_pack_manifest.py`, `test_manifest_loader_executes_entry_capability_and_budget_validation` |
| G2 second-round full-record gate | Every persisted string is redacted/rescanned; cron execution and mandatory assertion freshness/authority are fail-closed | `test_all_persisted_record_strings_are_redacted_and_occurrences_cursors_are_monotonic`, `test_cron_unknown_mandatory_and_stale_execution_are_unhealthy` |
| G2 second-round delivery/order | Bridge claims in-flight events exactly once; cursor and occurrence updates are monotonic; timeout lifecycle is explicit | `test_bridge_concurrent_drain_claims_each_event_once`, `test_asset_binding_deadline_and_snapshot_deep_freeze` |
| G2 release blockers | Complete sqlite object preflight, per-collection Cron reload, strict source cursor ordering, bootstrap identity/label binding, executable Review Pack factory, and bounded detached workers | `test_store_rejects_legacy_trigger_object_before_migration`, `test_cron_file_is_reparsed_and_duplicate_names_rejected`, `test_process_zero_match_and_launchd_label_mismatch_are_unhealthy`, `test_review_pack_factory_applies_runtime_target_and_budget` |
| P2 deep freeze/interpreter parity | Recursive immutable mappings detach snapshot/signal data; separate Python 3.14 environment contains dependencies | `test_asset_binding_deadline_and_snapshot_deep_freeze`; Python 3.14 command below |

## Fresh verification output

```bash
/Users/molly/Desktop/Hermes/venv/bin/python -m pytest -q tests/plugins/agentops
```

```text
114 passed in 3.53s
```

```bash
/private/tmp/agentops-py314.qSSO12/bin/python -m pytest -q -o addopts='' tests/plugins/agentops
```

```text
114 passed in 6.26s
```

The Python 3.14 environment is isolated at `/private/tmp/agentops-py314.qSSO12`;
it was created outside the main checkout and received test dependencies there,
without changing the main project environment.

```bash
/Users/molly/Desktop/Hermes/venv/bin/python -m pytest -q tests/hermes_cli/test_plugins.py tests/hermes_cli/test_plugin_cli_registration.py tests/hermes_cli/test_startup_plugin_gating.py tests/hermes_cli/test_plugin_scanner_recursion.py
```

```text
122 passed in 2.43s
```

```text
phase2 read-only boundary scan: PASS; matches=[]
target sqlite API scan: PASS; matches=[]
python 3.11 compileall: PASS
python 3.14 compileall: PASS
```

`git diff --check` exited with status 0.

## Known limitations

- This is library-only observer code: no production target configuration,
  daemon scheduling, live fleet collection or deployment is enabled.
- Target SQLite integrity is intentionally `unknown` while a target may have a
  live writer. Validation may only run on a separately authorized,
  AgentOps-owned copy in a later phase.
- A timed-out collector's Python worker cannot be force-killed safely; its
  caller returns a bounded unhealthy batch with `worker_detached=true` and
  continues. Phase 2 collectors contain no target-write primitive.
- Git dirty state remains `unknown`; the collector only reads direct Git
  metadata and refuses to infer a clean worktree.
- G2 still requires independent security/architecture review. No conclusion in
  this document authorizes merge, push or Phase 3.
