# Default Core Profile Read-Only Observation (G3 Gate)

**Status:** implementation/evidence contract only; seven-day production observation is pending.

**Sol-reviewed AgentOps implementation head:** `fbdc57bdd`. Subsequent PR
commits are documentation-only descendants; the AgentOps implementation tree
remains identical. Local `tests/plugins/agentops` passed 144 tests and local
`tests/plugins` passed 683 tests; compileall, static read-only scan, and diff
check also passed.

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

## Known limitations

- No seven-day online observation has run yet.
- Cron observation is optional and requires an already-authorized, bounded
  status asset; the loop does not discover or create one.
- The ledger is intentionally in-memory; process exit loses evidence unless an
  operator exports it through an approved external mechanism.
- No LLM, dashboard, notification delivery, automatic repair, or Phase 4 work
  is included.
