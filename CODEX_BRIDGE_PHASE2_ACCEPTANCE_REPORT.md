# Codex Bridge Phase 2 Acceptance Report

Date: 2026-08-22 (Asia/Saigon)

## Verdict

Phase 2 hardening code and isolated lifecycle acceptance are complete, but
Phase 2 is **not rollout-ready**. Two independent gates remain closed:

1. The Phase 1 24-hour checkpoint is not due until 2026-08-23 14:01 +07
   (preferred gate: 48 hours on 2026-08-24). The latest monitor definition is
   active, but no checkpoint observation has yet been appended to the Phase 1
   pilot report.
2. The projector's outcome-first Kanban prerequisite exists only in user-owned
   dirty files. Clean `HEAD` (`6f05267`) cannot satisfy the declared dependency
   contract. This is a reproducibility blocker even though the current dirty
   working tree passes the runtime probe.

No live config, process, card, or projection state was changed. The live profile
still has no `kanban_projection` section.

Phase 2 preserves the source-of-truth boundary: the Codex bridge database and
event stream are authoritative, while Kanban is a replayable projection. No
Codex phase transition awaits a Kanban write.

## Implemented surface

- `gateway/codex_bridge.py`: sends isolated in-memory wakes only after durable
  bridge events; owns one startup/retry worker with capped
  exponential backoff and bounded shutdown; projector construction fails closed.
- `gateway/run.py`: starts the optional drain worker with Gateway startup and
  stops it during graceful Gateway shutdown.
- `gateway/codex_kanban_projection.py`: explicit feature flag, durable queue,
  stable job/card mapping, claim lease, projection cursor, notification cursor,
  event receipts, outcome-first projection, needs-user projection, idempotent
  artifact mirroring, a versioned dependency probe, operator-readable read-only
  status, and read-only reconciliation CLI.
- `tests/gateway/test_codex_bridge.py`: verifies feature-off behavior and that a
  projector outage cannot prevent Codex from reaching `done`.
- `tests/gateway/test_codex_kanban_projection.py`: verifies config fail-closed,
  restart/dedupe, outcome-first state, needs-user action, outage/recovery,
  dashboard download bytes, and zero-write reconciliation.
- `scripts/run_codex_kanban_projection_canary.py`: isolated outage/recovery and
  artifact-delivery acceptance canary.
- `CODEX_BRIDGE_PHASE2_IMPLEMENTATION_PLAN.md`: architecture, state mapping,
  tests, SLOs, rollout, rollback, and Definition of Done.

No Telegram, Marrow, or Phase 3 surface was implemented. The dirty
Kanban/dashboard/plugin/config work owned by the user was not edited, reverted,
formatted, staged, or committed as part of Phase 2.

## Acceptance evidence

All suites were invoked through `scripts/run_tests.sh` with a unique
`--basetemp` for each invocation.

| Suite | Result |
| --- | ---: |
| Codex bridge, live opt-in | 19 passed |
| Kanban projection hardening | 10 passed |
| HTTP bridge | 2 passed |
| Process E2E, live opt-in | 1 passed |
| Packaging metadata | 7 passed |
| Gateway config | 61 passed |
| API server | 109 passed |
| **Executed total** | **209 passed, 0 failed** |

The non-live bridge invocation also produced 18 passed and one expected live
skip; the skipped test subsequently passed in the explicit live invocation and
is not double-counted above.

The isolated canary exited zero and reported:

```json
{
  "status": "pass",
  "codex_phase_during_outage": "done",
  "projection_error_recorded": true,
  "recovery_without_new_bridge_event": true,
  "manual_project_pending_calls_after_recovery": 0,
  "events_projected_after_recovery": 5,
  "projection_cursor": 5,
  "receipt_count": 5,
  "distinct_receipt_count": 5,
  "kanban_status": "done",
  "kanban_result": "Phase 2 canary complete",
  "artifact_bytes_verified": true
}
```

This proves the required independence property and closes the event-only wake
gap: an unavailable Kanban records a projection error but does not stop Codex;
the same service lifecycle automatically replays the durable backlog after
recovery without a new bridge event or a manual `project_pending()` call.

Focused behavior now explicitly covers feature-off inertness, startup backlog
drain, terminal-event outage, recovery without a new event, restart/dedupe, one
card/receipt/notification cursor, clean shutdown, artifact download bytes, and
zero-mutation reconciliation.

## Dependency readiness

The projector declares `hermes-kanban-outcome-first` contract version 1 and
verifies required callable signatures, states, and task columns. The current
working tree plus live Kanban database reports `dependency.ready: true`.

That runtime result is not reproducibility evidence. A read-only comparison to
clean `HEAD` found:

- `publish_task_output`: absent;
- `complete_task(..., with_reason=...)`: unsupported;
- `working` and `output_ready`: absent from `VALID_STATUSES`;
- `current_step`, `progress_percent`, `latest_log`, `files_changed`,
  `progress_updated_at`, and `block_kind`: absent from the clean task schema.

Phase 2 does not copy, stage, format, revert, or subsume those user-owned dirty
changes. Pilot rollout remains blocked until the prerequisite has an independent
landed revision or another explicitly owned, reviewable dependency boundary.

## Projection and notification semantics

- Bridge phases map to one stable card using
  `codex-bridge:<hermes_job_id>` as the idempotency key.
- `output_ready` persists the result before the projected card reaches `done`.
- Only public outcome data is projected. Prompt and private execution context
  remain in the authoritative bridge store.
- Every projected event gets one receipt. The notification cursor advances only
  for notification-eligible events; heartbeat remains a state update.
- Workspace-local artifacts within the existing attachment size limit are
  mirrored idempotently and served by the dashboard's existing one-click
  attachment download contract. The acceptance test compares served bytes to
  source bytes.

## Reconciliation evidence

The command below supports dry-run only and opens both SQLite databases in
read-only mode:

```powershell
venv\Scripts\python.exe -m gateway.codex_kanban_projection `
  --bridge-db C:\Users\ADMIN\AppData\Local\hermes\codex_bridge\state.db `
  --kanban-db C:\Users\ADMIN\AppData\Local\hermes\kanban.db --json
```

Against the live profile it reported three authoritative Phase 1 jobs, all
classified `missing_card`, with `mode: dry-run` and `mutations: 0`. This is the
expected pre-Phase-2 baseline because projection has never been enabled there.
The current rerun saw 146 existing Kanban cards and did not change them.

The new status command also ran read-only against the live databases and
reported 17 prospective pending events, cursor 0, receipt count 0, no last
error, and no retry scheduled. Both SQLite integrity checks returned `ok`.

## Live pilot and rollout gate

At the 2026-08-22 17:49 +07 snapshot,
`http://127.0.0.1:8642/health` returned HTTP 200 with `status: ok`; `hermes
gateway status --deep` passed 6/6 probes; PID 25964 listened only on
`127.0.0.1:8642`. The bridge DB had 3 terminal jobs, 0 active jobs, 3 distinct
threads, 0 duplicate idempotency keys, and 17 events. The live config remained
restricted to authenticated `api_server`, exactly one workspace, legacy worker
auto-dispatch off, and no `kanban_projection` section.

Earliest review for Phase 2 pilot activation remains after the 24-hour Phase 1
stability mark on 2026-08-23 14:01 +07. The preferred gate remains the 48-hour
mark on 2026-08-24. Passing the time gate alone is insufficient: dependency
readiness must also be reproducible from a landed revision. Only after both
conditions pass may rollout take the reversible config/restart/canary path.

Rollback is to set the flag false or remove the section and restart. Durable
bridge events and Codex execution remain intact; projected cards are audit
records and do not need destructive cleanup.
