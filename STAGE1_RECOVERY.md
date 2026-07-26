# Stage 1 Worker-Bridge Gateway Recovery

## Scope

This recovery restores the deleted gateway worker-bridge notifier/dispatcher
mixin and success-verification mixin. It does not add successor creation,
failure redispatch, database schema changes, dependencies, or worker-bridge
plugin changes.

## Files changed

- `gateway/worker_bridge_watchers.py`
  - Restores `GatewayWorkerBridgeWatchersMixin`.
  - Restores terminal transition alerts, pending-work alert text, idle nudges,
    guarded recovery of orphaned `queued` tasks, cursor persistence, and alert
    target resolution.
  - Leaves `created` tasks to `GatewayWorkerTaskDispatcherMixin`, preventing
    the two watchers from starting the same task.
  - Re-baselines a missing cursor or a cursor more than 1,000 events behind the
    current event head. Smaller restart windows remain eligible for delivery.
- `gateway/worker_bridge_ultra.py`
  - Restores `GatewayWorkerBridgeUltraMixin` and its post-success independent
    verification queue, receipt/crash artifacts, and internal gateway alert.
- `gateway/run.py`
  - Adds `GatewayWorkerBridgeWatchersMixin` to `GatewayRunner`.
  - Starts `_worker_bridge_notifier_watcher` as a supervised background task.
- `gateway/worker_task_dispatcher.py`
  - Unchanged. It remains the sole owner of `created` task starts.
- `hermes_cli/config.py`
  - Declares `worker_bridge.gateway_alerts` defaults for `enabled`,
    `interval_seconds`, `statuses`, `platform`, and `chat_id`.
- `tests/gateway/test_worker_bridge_watchers.py`
  - Covers imports/API surface, config keys, alert text, dispatch ownership,
    stale-cursor re-baselining, recent-cursor preservation, and notifier wake.
- `STAGE1_RECOVERY.md`
  - Records this recovery and its runtime verification signal.

## Decompiled versus reconstructed

The orphaned CPython 3.11 bytecode and audit supplied the recoverable module,
class, helper, method, configuration, cursor-file, alert-text, and ultra
artifact contracts. The Python source bodies were faithfully reconstructed
against the current gateway, session, worker-bridge, and message-event APIs.

The dispatch ownership split is a current-tree reconciliation rather than a
literal restoration: the newer `gateway/worker_task_dispatcher.py` owns
`created` tasks, while the restored rich watcher considers only orphaned
`queued` tasks. The 1,000-event startup cursor threshold is also an explicit
Stage 1 safety requirement.

## Verification

Run:

```text
python -m pytest tests/gateway -q
```

No gateway restart is performed by this recovery. After the operator next
restarts the gateway with `worker_bridge.gateway_alerts.enabled: true`, the
expected pickup log is:

```text
worker-bridge alerts: watching <hermes-home>\workers\bridge.db every 15s (statuses=failed,succeeded,timed_out)
```

The preceding cursor log should report:

```text
worker-bridge alerts: cursor ready at event <N>; backlogs over 1000 events are not replayed
```
