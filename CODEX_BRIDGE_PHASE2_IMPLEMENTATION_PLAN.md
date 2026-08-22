# Codex Bridge Phase 2 Implementation Plan

Date: 2026-08-22

Status: hardening code complete; rollout blocked by the Phase 1 time gate and an
uncommitted Kanban outcome-first prerequisite

## Hardening amendment — 2026-08-22 17:49 +07

The first acceptance pass exposed two gaps that this revision treats as release
blockers rather than papering over:

1. Clean `HEAD` (`6f05267`) does not satisfy the projection dependency contract.
   It has no `publish_task_output`, its `complete_task` lacks `with_reason`, its
   valid states lack `working` and `output_ready`, and its task schema lacks the
   outcome/progress columns consumed by the projector. Those prerequisites exist
   only in user-owned dirty Kanban work. Phase 2 now declares and probes the
   versioned `hermes-kanban-outcome-first` contract. A working-tree probe may be
   ready, but that does not make the Phase 2 source reproducible from `HEAD`.
   Rollout is blocked until that prerequisite is independently landed or another
   explicitly owned dependency boundary is supplied.
2. Event-only wakes could strand a terminal backlog forever after Kanban
   recovered. One persistent service worker now performs startup drain, serialized
   event wakes, capped exponential retry, autonomous retry without a new event,
   and bounded shutdown. Gateway startup and shutdown explicitly own this worker.

Operator status is available without target mutations:

```powershell
venv\Scripts\python.exe -m gateway.codex_kanban_projection `
  --bridge-db <bridge-state.db> --kanban-db <kanban.db> --status --json
```

It reports prospective/persisted pending count, projection and receipt cursors,
last error, retry count/state/next retry, and dependency readiness. Before the
projection tables exist, every durable bridge event is correctly reported as
prospective backlog rather than zero lag.

## 1. Outcome

Phase 2 turns the durable Phase 1 event stream into an optional Kanban read
model without making Kanban an executor, queue, lease owner, or delivery gate.
Codex execution remains authoritative and continues when the Kanban database is
missing, locked, read-only, corrupt, or otherwise unavailable.

The smallest slice covers the authenticated `api_server` pilot only. It does
not add Telegram, Discord, Marrow hooks, a worker fleet, or Phase 3 behavior.

## 2. Read-only audit evidence

### Git and ownership

- `HEAD` is Phase 1 commit `6f05267` and that commit contains the accepted 13
  Phase 1 files.
- The branch is locally ahead and remotely behind; Phase 2 will not fetch,
  rebase, merge, stage, or commit.
- The worktree contains user-owned edits in Kanban, dashboard, plugin, website,
  tests, and `hermes_cli/config_defaults.py`.
- Direct overlap exists in `hermes_cli/kanban_db.py`, `hermes_cli/kanban.py`,
  `gateway/kanban_watchers.py`, `plugins/kanban/dashboard/plugin_api.py`, and
  Kanban/dashboard tests. Phase 2 will consume their public behavior but will
  not edit, format, stage, or revert them.
- `.codex-bridge-pilot/write-canary-20260822.txt`, `bin/`, and
  `import-codex-auth.py` are excluded from the Phase 2 source surface.

### Runtime and data

- The pilot listener remains loopback-only on `127.0.0.1:8642`; `/health`
  returns HTTP 200.
- The Phase 1 bridge database passes `PRAGMA integrity_check` and currently
  holds 3 terminal jobs and 17 compact public events. No job is active.
- The Kanban database passes `PRAGMA integrity_check`; its current schema
  already exposes idempotency plus current-action, progress, log, files, and
  outcome-oriented fields through the user's in-progress Kanban changes.
- The Phase 1 event store already persists only public six-phase events and
  excludes private reasoning. It remains the source of truth.

## 3. Invariants

1. Codex append and execution never wait for a successful Kanban write.
2. A projection failure never changes a bridge job's phase or final result.
3. Kanban never starts, claims, resumes, retries, or completes Codex execution.
4. One bridge job maps to at most one Kanban card through a stable idempotency
   key.
5. One event is projected at most once logically; retries reconcile current
   target state before advancing the durable cursor.
6. The projection stores public summaries, origin identifiers already exposed
   by Phase 1, artifact paths, and final result only. It never stores prompts,
   replies, secrets, raw tool arguments, or reasoning.
7. Missing or invalid config fails closed: projection is off.
8. Feature-off behavior does not import or initialize the Kanban database.
9. The live pilot config is not changed during implementation.

## 4. State and data ownership

```text
CodexBridgeService
    -> bridge_jobs + bridge_events       authoritative execution state
    -> best-effort projection wake       non-blocking, failure-isolated

CodexKanbanProjector
    -> bridge_projection_jobs            job/card mapping + cursors
    -> bridge_projection_receipts        per-event durable dedupe ledger
    -> Kanban tasks/task_events/runs      rebuildable read model
```

Execution state machine remains:

```text
captured -> working -> needs_user -> working -> output_ready -> done
                         |                           |
                         +---------- failed <-------+
```

Projection mapping:

| Bridge phase | Kanban projection | Notification policy |
| --- | --- | --- |
| `captured` | Create/reuse one `working` card | eligible once |
| `working` | Update current action/heartbeat-age source timestamp | no heartbeat notification |
| `needs_user` | Set concrete `Needs You` action | eligible immediately once |
| resumed `working` | Clear the needs-user presentation and show current action | not eligible |
| `output_ready` | Persist outcome summary and artifact paths before review/done | eligible once |
| `done` | Mark projection terminal after output is already visible | only eligible if it adds information |
| `failed` | Persist failure class and concrete next action | eligible once |

The card title is a bounded stable label based on the bridge job ID. The card
body contains only executor, origin type/conversation, and workspace metadata;
the original prompt is deliberately absent because Phase 1 does not persist it.

## 5. Durable cursor and dedupe

Three additive tables live in the Phase 1 SQLite database so they survive Gateway
restart and do not depend on Kanban availability:

```sql
bridge_projection_queue(
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id UNIQUE,
  hermes_job_id,
  created_at
)

bridge_projection_jobs(
  hermes_job_id PRIMARY KEY,
  kanban_task_id,
  projection_cursor,
  notification_cursor,
  last_error,
  updated_at
)

bridge_projection_receipts(
  event_id PRIMARY KEY,
  hermes_job_id,
  notification_eligible,
  projected_at
)
```

An additive trigger assigns a durable sequence when a bridge event is inserted;
initialization backfills pre-Phase-2 events once in their original insertion
order. Pending work is selected by event ID against the receipt ledger. The
explicit cursor is an operator-visible watermark, while the receipt primary key
is the authoritative dedupe guard. Steady-state ordering therefore does not
rely on timestamps or SQLite `rowid` across clock changes or maintenance.

Crash recovery protocol:

1. Read the oldest unreceipted public event.
2. Create/reconcile the stable Kanban card.
3. Apply an idempotent state projection.
4. Only after the target state is confirmed, insert the event receipt and move
   the projection/notification cursor in one source-DB transaction.
5. If a crash occurs after target commit but before cursor commit, the retry
   observes the already-reconciled target state, performs no duplicate logical
   transition, and then advances the cursor.

## 6. Feature flag

The vertical slice reads raw `config.yaml` fail-closed and does not edit the
dirty default-config file:

```yaml
kanban_projection:
  enabled: false
  board: default
```

Only `enabled: true` activates the projector. Missing section, YAML failure, an
empty/invalid board, or any initialization exception leaves it disabled. The
existing Phase 1 pilot remains off because its config has no section.

## 7. Exact implementation surface

New files:

- `gateway/codex_kanban_projection.py`
  - fail-closed settings loader;
  - additive projection schema;
  - pending-event scan and durable cursor/dedupe;
  - stable card creation and outcome-first phase projection;
  - bounded/redacted target payloads;
  - failure isolation and retry state.
- `tests/gateway/test_codex_kanban_projection.py`
  - config gate, schema, mapping, cursor, dedupe, restart, outcome-first data,
    needs-user action, and outage tests using temporary bridge/Kanban DBs.

Scoped edits to clean Phase 1 files:

- `gateway/codex_bridge.py`
  - optional projector injection;
  - lazy default construction only when the flag is enabled;
  - fire-and-forget projection wake after durable event append;
  - retain background task references and consume exceptions for clean asyncio
    lifecycle behavior.
- `tests/gateway/test_codex_bridge.py`
  - service-level proof that a projector exception cannot stop Codex or alter
    the authoritative event/final-result lifecycle.
- `gateway/run.py`
  - start the optional drain worker with the Gateway and stop it cleanly during
    Gateway shutdown; the default-off path remains inert.

No edit is planned to Kanban DB, Kanban CLI, dashboard source/dist, watcher,
plugin, config defaults, pilot config, Phase 1 canary artifact, or auth helper.

## 8. Vertical-slice behavior

The projector is a pull consumer. Gateway startup starts exactly one persistent
worker and immediately scans the durable backlog. Every newly appended bridge
event performs a local in-memory wake only. Wakes are serialized; failure enters
exponential backoff capped by `retry_max_seconds`, and the same worker retries
after recovery even when no later bridge event arrives.

The execution coroutine does not await projection completion. The only
in-process coupling is scheduling a local coroutine/task. The authoritative
SQLite append happens first.

Kanban projection is outcome-first:

- `output_ready` persists the final result and artifact list before the card is
  terminal;
- `done` does not erase or replace that outcome;
- `needs_user` always uses the structured public question already emitted by
  Phase 1 and its prompt ID as the concrete action;
- working updates overwrite the cheap card read model instead of appending an
  unbounded comment stream.

## 9. Tests and acceptance criteria

All Python tests run through `scripts/run_tests.sh` with a unique repo-local
`--basetemp` for each invocation.

Required focused tests:

1. Flag absent/false performs no Kanban import/open/write.
2. Six-phase validation remains unchanged.
3. Duplicate capture/event wake creates one card and one receipt per event.
4. Projector restart reuses mapping and resumes after the stored cursor.
5. `needs_user` contains a concrete question/action and prompt ID.
6. `output_ready` stores result before `done` and keeps artifact paths.
7. Working projection exposes current action and an update timestamp from which
   heartbeat age can be calculated.
8. Reprocessing an event after simulated crash does not duplicate the logical
   card transition.
9. Locked/unavailable Kanban records a retryable projection error while Codex
   reaches `output_ready -> done` with the correct final result.
10. Projection recovery after outage drains all unreceipted events in order.
11. Phase 1 focused suites remain green.
12. Scoped `git diff --check` passes for Phase 2-owned files.

Acceptance maps directly to the architecture plan:

- reload/restart does not duplicate notification cursor advancement;
- card exposes current action and update/heartbeat age inputs;
- `Needs You` is concrete;
- artifacts are present as direct paths in the outcome payload;
- Kanban outage does not stop or fail Codex.

## 10. Rollout

1. Land code with the flag absent/off.
2. Run unit and service integration tests with temporary databases.
3. Run a local outage canary by pointing projection at an unavailable/read-only
   target while a fake/live-safe Codex request completes.
4. Enable projection only for the current `api_server` workspace after the open
   Phase 1 stability window is reviewed.
5. Observe cursor lag, projection failures, duplicate-card count, and Codex
   completion independently.
6. Only then enable the real default-board projection for one pilot request.

No Telegram or Marrow rollout is part of these steps.

## 11. Rollback

- Set `kanban_projection.enabled: false` and restart the Gateway.
- Codex Bridge Phase 1 continues unchanged; no source revert or Kanban cleanup is
  required.
- Projection tables and projected cards remain as audit history; do not rewrite
  or delete them during rollback.
- If a card is stale, disable projection and use a later dry-run reconciliation
  tool rather than manually mutating authoritative bridge events.

## 12. SLOs and observability

Initial targets for the slice:

| Signal | Target |
| --- | ---: |
| Added synchronous Codex critical-path latency | effectively 0; scheduling only |
| Lost authoritative bridge events/results | 0 |
| Duplicate cards per bridge job | 0 |
| Duplicate notification-eligible receipts per event | 0 |
| Projection catch-up after Kanban recovery | autonomous bounded retry, under 5 seconds in tests |
| Projected public summary length | at most 500 characters |
| Projector failure effect on Codex phase | none |

Operator-visible state includes last projection cursor, notification cursor,
last error, and update time. Heartbeats are persisted as state changes but are
not notification-eligible.

## 13. Legacy reconciliation dry-run

The completed Phase 2 slice includes a read-only reconciliation command. It
opens both SQLite databases in read-only mode and classifies:

- exact stable-id match;
- probable legacy match requiring review;
- orphan projection;
- authoritative bridge job with no card;
- duplicate cards.

Run it with:

```powershell
venv\Scripts\python.exe -m gateway.codex_kanban_projection `
  --bridge-db <bridge-state.db> --kanban-db <kanban.db> --json
```

Dry-run is the only supported mode. The report always includes
`"mode": "dry-run"` and `"mutations": 0`; no apply mode exists in this
slice. `--fail-on-findings` is available for CI or operator gating.

## 14. Artifact delivery contract

On `output_ready`, workspace-local artifacts within the Kanban attachment size
limit are mirrored through the existing attachment store. Uploads use a stable
projection marker derived from the event and canonical source path, so retry or
restart does not create another logical attachment. The dashboard's existing
attachment download endpoint then provides a one-click file path without
placing Kanban on the Codex execution critical path.

The isolated acceptance canary is:

```powershell
venv\Scripts\python.exe scripts\run_codex_kanban_projection_canary.py
```

It deliberately starts with an unavailable Kanban target, proves the Codex job
still reaches `done`, restores Kanban, drains the durable backlog, and verifies
the final result, cursor/receipt dedupe, and downloaded artifact bytes.

## 15. Definition of Done for this slice

- Detailed audit and plan are committed to the working tree as Phase 2-owned
  documentation.
- Feature-off path is inert and preserves the live pilot.
- Durable projection mapping, receipt dedupe, and both cursors exist.
- One complete fake Codex lifecycle produces one outcome-first Kanban card.
- Restart and duplicate wakes do not create a second card or duplicate receipt.
- A concrete needs-user action and artifact paths survive projection.
- A deterministic Kanban outage test proves Codex still reaches `done`.
- Reconciliation is read-only by construction and reports drift without writes.
- Workspace-local artifacts use the existing one-click attachment contract.
- Focused Phase 1 and Phase 2 suites pass via the mandatory wrapper.
- No user-owned dirty file is edited, staged, reverted, committed, or formatted.
