# Default Core Profile Read-Only Observation (G3 Gate)

**Status:** implementation/evidence contract only; seven-day production observation is pending.

**Sol-reviewed AgentOps implementation baseline:** `fbdc57bdd`. The bounded
runbook hardening in this candidate is a code descendant (it changes
`observation.py` and the log collector); it is not a documentation-only
descendant and requires independent review. The latest local
`tests/plugins/agentops` passed 155 tests and local `tests/plugins` passed 694
tests; final execution results are recorded with the candidate commit.

## Scope and trust boundary

Only `hermes:profile:default:gateway` is core scope. The loop reads the fixed
`~/Library/LaunchAgents/ai.hermes.gateway.plist` deployment asset through an
`O_NOFOLLOW` descriptor, verifies regular-file type, current-user ownership,
single-link identity, bounded size, expected Label, and the exact command
fingerprint recorded by the registry. `feishu3`, `feishu4`, `feishu5`, and
`newbot` remain registered but are out of scope and do not affect the G3
denominator.

The loop invokes only read-only Process, Launchd plist, regular-file Log, and
optionally supplied Cron status collectors. It does not call `launchctl`,
restart/stop/start, edit Gateway/LaunchAgent/Cron/Target/business data, or
install a scheduler. Phase 2 SQLite persistence remains deferred; the only
production sink in this loop is the bounded in-memory `ObservationLedger`.

## Natural seven-day run

An operator runs one explicit `DefaultObservationLoop.collect_once()` at the
approved cadence (the review-pack rate limits are applied to each collector),
retaining the process in the operator's existing supervision environment. No
LaunchAgent or Cron installation is part of this change. Each pass appends a
detached, recursively redacted `CollectionBatch` to the bounded ledger and
updates only in-memory cursors/snapshots.

At the end of each UTC day, the operator exports `daily_summary(day)` and the
bounded `terra_input(day)` for offline analysis. Terra input has no actions and
is not an LLM call; it is a redacted, size/item-bounded analysis handoff.

## Two-stage operating runbook

1. **Day 1 backlog drain (not an observation day):** invoke
   `ObservationRunbook.drain_backlog(max_passes=...)`. The runbook is a one-shot
   `DRAINING → READY → OBSERVING` state machine: only the initial DRAINING
   stage may drain, and a verified finalize transitions it to OBSERVING.
   Each invocation performs
   at most one eligible collection pass: the next pass is safe-stopped until
   every collector's review-pack rate interval (at least 60 seconds in the
   default pack) is eligible. It uses the existing loop cursor and bounded
   review-pack limits, and stops when the log cursor reaches the current file
   tail or when a no-follow dev/inode identity, ledger, asset, or collector
   boundary fails. A backlog report carries
   `observation_day_counted=false`, `passes`, `tail_reached`,
   `next_eligible_seconds`, `stop_reason`, and ledger counters. Missed slots are
   not replayed as bursts.
2. **Daily observation rotation:** after a UTC day's `daily_summary` and full
   bounded `terra_input` have been exported and independently validated,
   invoke `rotate_after_daily_export` only from OBSERVING. The operation recomputes the current
   UTC summary/Terra envelope, checks the redaction and 8 MiB/item budgets, and
   requires a SHA-256 receipt over that exact canonical envelope. It replaces
   only the in-memory ledger; the same loop instance and cursor map are
   preserved. If the receipt, current-ledger binding, UTC day, redaction, or
   budget checks fail, rotation is rejected and the old ledger remains
   authoritative.
   After backlog reaches a verified tail, `finalize_backlog_export` performs the
   same receipt check, clears the Day0 ledger with `observation_day_counted=false`,
   preserves cursors, and reports the next UTC observation day. Finalization is
   one-shot; a second finalize or a second backlog drain is rejected.
3. **Missed slot:** invoke `record_missed_slot(scheduled_at)` for metadata only;
   it records `catch_up=false`, `slot_satisfied=false`, and
   `day_success_eligible=false` and performs no collection. Future and duplicate
   slots are rejected; a missed slot makes that UTC day ineligible for export.
   Finalize records the next eligible UTC day, so same-day/future receipts cannot
   be replayed as observation days. Every event records UTC timestamp/day, target,
   collectors, ledger counters, and stage status in a detached bounded metadata
   record.

## P0/P1 validation and exit conditions

- P0 injection: secret-bearing or malformed signal evidence is rejected before
  ledger mutation; poisoned payloads cannot enter the Terra handoff.
- P1 deployment binding: missing/disabled/default-profile label changes,
  owner/mode/link changes, symlink swaps, malformed assets, and command
  fingerprint mismatches fail closed before collection.
- A successful seven-day observation requires all seven UTC daily summaries,
  no unexplained ledger overflow, valid redaction/binding checks on every run,
  and an explicit review of unhealthy/stale/unknown collector reasons. This
  implementation does not claim those conditions until the operator performs
  the run.

Rollback is stopping invocation of the loop and discarding the in-memory
ledger. There is no target rollback because this feature performs no target or
production writes.

## Offline evidence

| Area | Evidence |
|---|---|
| Bounded append-only sink, detached authority record, secret gate, budgets | `test_ledger_is_append_only_detached_and_bounded`, `test_ledger_authority_record_survives_source_mutation` |
| Daily UTC summary and complete Terra envelope budget | `test_daily_summary_and_terra_input_are_bounded_and_no_actions`, `test_daily_summary_uses_utc_day_label` |
| Read-only Process/Launchd/Log/Cron collection and unchanged input hashes | `test_default_loop_collects_read_only_process_launchd_logs_and_cron` |
| Fixed asset/fingerprint/label fail-closed binding on every pass | `test_default_loop_rejects_tampered_or_disabled_binding`, `test_launchd_asset_replacement_between_passes_fails_closed` |
| No SQLite/lifecycle surface | `test_default_loop_does_not_expose_sqlite_or_lifecycle_surface` |
| Two-stage backlog/rotation/missed-slot protocol | `test_runbook_backlog_drain_reaches_tail_without_counting_observation_day`, `test_runbook_daily_rotation_requires_export_and_preserves_cursor`, `test_runbook_export_receipt_is_bound_to_current_ledger`, `test_runbook_backlog_stops_on_ledger_budget`, `test_runbook_rate_limit_is_safe_stop_not_catch_up`, `test_runbook_rejects_inode_changed_tail`, `test_runbook_rejects_path_rotation_before_declaring_tail`, `test_runbook_finalize_backlog_requires_verified_export_and_marks_day0`, `test_runbook_rejects_mixed_utc_days_and_reserves_rotation_event`, `test_runbook_capacity_and_slot_reservations_fail_before_mutation`, `test_runbook_rejects_future_duplicate_slots_and_metadata_mutation` |

## Known limitations

- No seven-day online observation has run yet.
- Cron observation is optional and requires an already-authorized, bounded
  status asset; the loop does not discover or create one.
- The ledger is intentionally in-memory; process exit loses evidence unless an
  operator exports it through an approved external mechanism.
- No LLM, dashboard, notification delivery, automatic repair, or Phase 4 work
  is included.
