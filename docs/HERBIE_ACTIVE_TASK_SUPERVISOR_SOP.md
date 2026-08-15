# Herbie Active Task Supervisor SOP

## Purpose

The Active Task Supervisor is a deterministic control plane for consequential owner-assigned tasks. Its goal is to ensure a task cannot silently disappear while Herbie is blocked, stale, out of tool budget, or ready for review.

The Herbie Execution Operating Charter remains authoritative for execution discipline. This SOP describes the runtime state, watchdog behavior, and review/activation process.

## Runtime storage

Default private state location:

- Ledger: `$HERMES_HOME/task-supervisor/active_task.json`
- Event log: `$HERMES_HOME/task-supervisor/events.jsonl`
- Notification dedupe: `$HERMES_HOME/task-supervisor/dedupe_state.json`

The runtime directory is private operational state and must not be committed to public repositories. Do not place secrets, Steve's private contact details, race-director emails, prospect data, or customer data in the ledger.

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

## One active owner task by default

Only one consequential Steve-assigned task may be `ACTIVE` by default. If another consequential assignment arrives while an existing task is `ACTIVE`, `BLOCKED`, or `WAITING_OWNER`, record the new task as `QUEUED`, notify Steve, and do not interleave the work unless Steve explicitly authorizes parallel execution.

## Manual task start procedure

1. Read the authoritative specification in full.
2. Compute and record the spec SHA-256 when a file is supplied.
3. Create a ledger entry in `$HERMES_HOME/task-supervisor/active_task.json` with status `PREFLIGHT`.
4. Append a `state_transition` event to `events.jsonl`.
5. Send Steve a `TASK ACCEPTED` preflight acknowledgement within 5 minutes.
6. Transition to exactly one of:
   - `ACTIVE` and continue implementation;
   - `BLOCKED` and immediately notify Steve;
   - `WAITING_OWNER` and ask the required decision question.

Preflight cannot end silently.

## Communication SLA

- Assignment acknowledgement: within 5 minutes.
- Blocker notification target: within 5 minutes of blocker detection.
- Active task heartbeat/milestone: at least every 45 minutes while `ACTIVE`.
- Completion/review notification: immediately when `READY_FOR_INDEPENDENT_REVIEW` or `COMPLETE`.

## Watchdog

Script entrypoint:

```bash
python3 scripts/herbie_task_supervisor_watchdog.py
```

Recommended disabled cron manifest:

- Schedule: `*/15 * * * *`
- Mode: `no_agent: true`
- Delivery: `origin`
- Script: `herbie_task_supervisor_watchdog.py`

Healthy/no-change runs print nothing and exit 0. Script-only cron delivery sends non-empty stdout to the configured owner destination. Non-zero exit surfaces through scheduler failure handling.

## Watchdog behavior

Each run:

1. reads the active task ledger;
2. exits silently if no monitored task exists;
3. validates fixed state and required provenance fields;
4. detects stale `PREFLIGHT` tasks;
5. records one internal nudge event for `ACTIVE` tasks stale for 30 minutes;
6. emits an owner heartbeat when `ACTIVE` has no owner update for 45 minutes;
7. emits one critical stale alert when an `ACTIVE` task is stale for 60 minutes;
8. emits one blocked alert for `BLOCKED` tasks that have not been marked delivered;
9. emits one recovery notice when a previously blocked task returns to `ACTIVE`;
10. emits one review/complete notice for `READY_FOR_INDEPENDENT_REVIEW` or `COMPLETE`;
11. dedupes repeated incidents by task/status/fingerprint.

## Internal nudge capability

The code records deterministic internal nudge events and the exact nudge text:

`TASK SUPERVISOR: update task state, checkpoint progress, and continue or declare BLOCKED.`

No reliable approved non-LLM auto-resume transport is assumed in this implementation. If one is later configured, invoke the watchdog with `--internal-nudge-command-available` and wire the approved command outside this public-safe implementation. Until then, stale-owner alerting remains the backstop.

## Notification delivery state

The watchdog's default transport is stdout so Hermes script-only cron can deliver through the existing scheduler. The script records `owner_notification_emitted` and sets task notification state to `emitted_pending_transport`. It does not mark `delivered` unless an external transport-success callback or manual closeout records a delivered event after successful delivery. This avoids faking delivery success.

## Tool-budget/session-limit stop

If execution cannot safely continue because of tool/session limits:

1. export diff/state and checkpoint any work;
2. update the task ledger to `BLOCKED` or `READY_FOR_INDEPENDENT_REVIEW`;
3. record the checkpoint commit/artifact path;
4. notify Steve immediately;
5. do not leave the task `ACTIVE`.

## Activation steps after independent review

Do not activate before review. After approval:

1. Ensure the reviewed code is merged/deployed.
2. Install or expose `scripts/herbie_task_supervisor_watchdog.py` in the runtime checkout used by Hermes cron.
3. Create the scheduler job from `cron-manifests/herbie_active_task_supervisor.disabled.json` with `enabled: true` only after Steve approval.
4. Confirm the job appears as scheduled and remains script-only/no-agent.
5. Run one no-task smoke: it must exit 0 silently.
6. Create a synthetic blocked-task fixture in a temporary ledger directory and verify stdout shape without touching production ledger.
7. Record activation evidence in the private event log.

## Rollback

1. Pause or remove only the Active Task Supervisor cron job.
2. Do not modify the StartLine audit-request watcher.
3. Do not modify the StartLine Phase 3B-1 transaction reconciliation monitor.
4. Preserve `$HERMES_HOME/task-supervisor/active_task.json`, `events.jsonl`, and `dedupe_state.json` for forensic review.
5. If rollback is due to false alerts, archive the dedupe state with a timestamp before restarting.

## Explicit non-goals

This supervisor does not authorize Candidate 17 work, prospect sourcing, mockup creation, race-director outreach, form submissions, production marketing data writes, Phase 2A-1C, or Phase 2A-2. It also must not alter existing StartLine audit-request watcher or transaction reconciliation monitor jobs.
