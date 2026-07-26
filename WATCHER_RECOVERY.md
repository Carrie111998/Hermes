# Worker-Bridge Watcher Recovery

## Scope

This recovery restores only the standalone gateway worker-bridge watcher
module. It is intentionally not wired into `GatewayRunner`; wiring, gateway
restart, configuration defaults, ultra verification, plugin changes, and
database schema changes remain outside this task.

## Restored behavior

- `GatewayWorkerBridgeWatchersMixin` provides:
  - `_worker_bridge_notifier_watcher`
  - `_worker_bridge_tick`
  - `_worker_bridge_auto_dispatch`
  - `_worker_bridge_idle_nudge`
  - `_resolve_worker_alert_target`
- Terminal `task.status` events are collected for `failed`, `succeeded`, and
  `timed_out`.
- `workers/gateway_alerts_cursor.json` is initialized at the current event
  head. A cursor more than 1,000 events behind is also moved to the head, while
  a recent cursor is preserved.
- Rich auto-dispatch considers only orphaned `queued` tasks. It never selects
  or claims `created` tasks, which remain owned by
  `gateway/worker_task_dispatcher.py`.
- Alert text reports terminal outcomes, includes pending work, and instructs
  the agent to triage failures before starting appropriate work with
  `hermes worker tasks start <task_id>`.
- `worker_bridge.gateway_alerts` reads `enabled`, `interval_seconds`,
  `statuses`, `platform`, and `chat_id`. Alerts remain disabled when the
  section is absent.

## Files

- `gateway/worker_bridge_watchers.py`
- `tests/gateway/test_worker_bridge_watchers.py`
- `WATCHER_RECOVERY.md`

## Verification

```text
python -m pytest tests/gateway/test_worker_bridge_watchers.py -q
```

Final result: `9 passed in 0.43s`.
