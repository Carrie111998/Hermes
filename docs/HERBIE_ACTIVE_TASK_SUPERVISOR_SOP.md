# Herbie Active Task Supervisor SOP

## Purpose

The Active Task Supervisor is a deterministic control plane for consequential owner-assigned tasks. It prevents silent disappearance while Herbie is unacknowledged, blocked, waiting for owner, stale, out of tool budget, queued behind another task, ready for review, complete, or aborted.

The Herbie Execution Operating Charter remains authoritative for execution discipline. This SOP describes the runtime state, watchdog behavior, review/activation process, rollback, and recovery lifecycle.

## Runtime storage

Default private state location:

- Canonical task store: `$HERMES_HOME/task-supervisor/tasks.json`
- Legacy active-task mirror: `$HERMES_HOME/task-supervisor/active_task.json`
- Event log: `$HERMES_HOME/task-supervisor/events.jsonl`
- Notification outbox: `$HERMES_HOME/task-supervisor/notification_outbox.json`
- Incident/blocker lifecycle state: `$HERMES_HOME/task-supervisor/dedupe_state.json`
- Watchdog lock: `$HERMES_HOME/task-supervisor/.watchdog.lock`

`tasks.json` preserves every task:

```json
{
  "active_task_id": "...",
  "tasks": { "<task_id>": { } },
  "queue": ["<queued_task_id>"]
}
```

Queued task insertion must never overwrite the active task. Queue promotion is deterministic and owner-controlled unless a later reviewed policy explicitly permits automatic promotion.

The runtime directory is private operational state and must not be committed to public repositories. Do not place secrets, race-director emails, prospect data, customer data, or production payloads in the ledger.

## Task ingestion

Ordinary chat is not automatically supervised. Consequential work must enter through the supervised task-ingestion path.

Entrypoint:

```bash
python3 scripts/herbie_task_supervisor_task.py start \
  --task-id HERBIE-YYYYMMDD-HHMM-slug \
  --title "Task title" \
  --owner owner \
  --spec-path /absolute/path/to/spec.md \
  --spec-version "version label"
```

The entrypoint:

1. computes and records spec SHA-256;
2. creates a task ID supplied by the caller;
3. preserves any active task;
4. safely queues a second task unless `--parallel-authorized` is supplied;
5. records RECEIVED/QUEUED state and event history.

Owner-controlled queue promotion:

```bash
python3 scripts/herbie_task_supervisor_task.py promote-next
```

## Schemas

Public-safe schemas are committed for review:

- `schemas/herbie_active_task.schema.json`
- `schemas/herbie_active_task_event.schema.json`

Fixed task states:

- `RECEIVED`
- `PREFLIGHT`
- `ACTIVE`
- `BLOCKED`
- `WAITING_OWNER`
- `READY_FOR_INDEPENDENT_REVIEW`
- `COMPLETE`
- `ABORTED`
- `QUEUED`

A task may not stop while still `ACTIVE`. Before stopping, transition to `BLOCKED`, `WAITING_OWNER`, `READY_FOR_INDEPENDENT_REVIEW`, `COMPLETE`, or `ABORTED`.

## Communication SLA

- Assignment acknowledgement/preflight transition: within 5 minutes of `RECEIVED`.
- Blocker notification target: within 5 minutes of blocker detection.
- `WAITING_OWNER`: immediate actionable owner-decision message.
- Active task heartbeat/milestone: at least every 45 minutes while `ACTIVE`.
- Critical stale: one owner attention alert when an active task has no progress for 60 minutes.
- Ready/complete/aborted closeout: exactly one transport-confirmed closeout/review message.
- Queued task: exactly one queue notice identifying queued task, active task, reason, and owner action.

## Watchdog

Script entrypoint:

```bash
python3 scripts/herbie_task_supervisor_watchdog.py --transport send-message --owner-target ${HERMES_TASK_SUPERVISOR_OWNER_TARGET}
```

Disabled cron manifest:

- Schedule: `*/15 * * * *`
- Mode: `no_agent: true`
- Delivery: `local`
- Script: absolute reviewed script path plus `--transport send-message --owner-target telegram`
- Workdir: exact reviewed runtime checkout
- State: disabled/paused until owner + independent review approval

Healthy/no-change runs print nothing and exit 0. Successful owner notifications are sent by the deterministic transport adapter, not by cron stdout, so cron delivery is local to avoid duplicate messages. Non-zero exit surfaces transport or state failure.

## Transport-confirmed delivery

The watchdog uses a durable outbox record per incident key:

- notification created;
- notification attempted;
- notification delivered;
- notification failed/retryable.

The incident is not permanently deduped until the owner transport returns success. On transport failure the outbox remains pending/failed-retryable, task `last_owner_notification_attempt_at` may update, `last_owner_update_at` does **not** update, and a later run retries. Successful delivery updates `last_owner_update_at` and `last_owner_notification_delivered_at`.

Approved V1 transport is Hermes' reviewed `send_message` path, pinned to owner's Telegram owner path by `--owner-target telegram` unless activation review approves a more specific target. Stdout-confirmed transport is only for tests/manual local validation and is not the recurring cron transport.

## Watchdog behavior

Each run under a process lock:

1. reads `tasks.json`, outbox, dedupe/blocker lifecycle state;
2. exits silently if no tasks exist;
3. validates fixed state and required provenance fields;
4. handles `RECEIVED`, `PREFLIGHT`, `ACTIVE`, `BLOCKED`, `WAITING_OWNER`, `READY_FOR_INDEPENDENT_REVIEW`, `COMPLETE`, `ABORTED`, and `QUEUED`;
5. records one internal nudge event for `ACTIVE` tasks stale for 30 minutes;
6. emits owner heartbeat only after confirmed delivery, then updates the 45-minute owner clock;
7. emits one critical stale alert when an `ACTIVE` task is stale for 60 minutes;
8. maintains blocker episode lifecycle: opened, delivered, resolved, recovery delivered;
9. emits exactly one recovery notice per resolved blocker episode;
10. retries failed transport attempts without duplicate successful deliveries.

Malformed JSON/state fails closed and exits nonzero. Event appends are append-only and protected by the watchdog transaction lock.

## Internal nudge capability

The code records deterministic internal nudge events and the exact nudge text:

`TASK SUPERVISOR: update task state, checkpoint progress, and continue or declare BLOCKED.`

V1 does not auto-resume Herbie. The runtime records:

- `internal_nudge_recorded`
- `auto_resume_status = NOT_CONFIGURED`

Owner heartbeat/stale notification is the V1 backstop. Automatic resumption is a future enhancement.

## Tool-budget/session-limit stop

If execution cannot safely continue because of tool/session limits:

1. export diff/state and checkpoint any work;
2. update the task ledger to `BLOCKED` or `READY_FOR_INDEPENDENT_REVIEW`;
3. record checkpoint commit/artifact path;
4. notify owner immediately;
5. do not leave the task `ACTIVE`.

## Activation steps after independent review

Do not activate before review. After approval:

1. Use the exact reviewed local/fork commit.
2. Ensure `cron-manifests/herbie_active_task_supervisor.disabled.json` has `reviewed_commit` set to that exact commit, a clean reviewed `workdir`, and an absolute reviewed script path.
3. Create the scheduler job only after operator approval.
4. Confirm `no_agent: true`, schedule `*/15 * * * *`, `deliver: local`, and `--transport send-message`.
5. Run no-task smoke: exit 0 silently.
6. Run temporary synthetic owner-transport validation with messages labeled `HERBIE TASK SUPERVISOR TEST — No action required`.
7. Record activation evidence in the private event log.

## Rollback

1. Pause or remove only the Active Task Supervisor cron job.
2. Do not modify the existing private audit-request watcher.
3. Do not modify the existing private transaction reconciliation monitor.
4. Preserve `$HERMES_HOME/task-supervisor/*` for forensic review.
5. If rollback is due to false alerts, archive `notification_outbox.json` and `dedupe_state.json` with a timestamp before restarting.

## Explicit non-goals

This supervisor does not authorize Candidate 17 work, prospect sourcing, mockup creation, race-director outreach, form submissions, production marketing data writes, Phase 2A-1C, or Phase 2A-2. It also must not alter existing existing private audit-request watcher or transaction reconciliation monitor jobs.
