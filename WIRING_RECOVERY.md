# Worker-Bridge Watcher Wiring Recovery

## Changed files

- `gateway/run.py`
  - Adds `GatewayWorkerBridgeWatchersMixin` to `GatewayRunner` before
    `GatewayWorkerTaskDispatcherMixin`.
  - Keeps the existing created-task dispatcher supervised at startup.
  - Starts the recovered notifier/queued-task watcher only when
    `worker_bridge.gateway_alerts.enabled` is exactly `true`.
- `tests/gateway/test_worker_bridge_watcher_wiring.py`
  - Verifies the mixin MRO, enabled and disabled scheduling, single startup
    registration, and preservation of the created-task dispatcher hook.
- `WIRING_RECOVERY.md`
  - Records activation, verification, and rollback details.

## Verification

Command:

```text
C:\Python314\python.exe -m pytest tests/gateway/test_worker_bridge_watcher_wiring.py tests/gateway/test_worker_task_dispatcher.py -q
```

Test output:

```text
......                                                                   [100%]
6 passed in 1.28s
```

## Expected gateway startup log

With `worker_bridge.gateway_alerts.enabled: true`, the supervised watcher logs
its initialized cursor and then its active polling configuration:

```text
worker-bridge alerts: cursor ready at event <event_id>; backlogs over 1000 events are not replayed
worker-bridge alerts: watching <HERMES_HOME>\workers\bridge.db every <interval>s (statuses=<statuses>)
```

When the setting is false or absent, neither watcher log line is expected.
The existing created-task dispatcher remains active independently.

## Activation

A gateway restart is required for this startup wiring to take effect. This
recovery task does **not** restart the gateway.

## Rollback

Revert the `GatewayWorkerBridgeWatchersMixin` import and inheritance entry,
replace `self._start_worker_bridge_watchers()` with the original single
`_worker_task_dispatcher_watcher` supervised startup call, remove
`_start_worker_bridge_watchers`, and remove the wiring test file. No config,
database, dependency, or plugin rollback is required.
