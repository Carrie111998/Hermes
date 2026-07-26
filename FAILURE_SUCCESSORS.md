# Failure Successors

The gateway worker-bridge watcher can create a fresh task after a terminal
failure has exhausted its same-task auto-repair budget. This gives the work a
clean runtime while keeping the retry chain finite and traceable.

## Design

On each watcher cycle, Hermes reads tasks through the worker bridge and selects
`failed` or `timed_out` tasks whose
`runtime.auto_repair_attempts >= metadata.auto_repair`. It creates the successor
only through the public `WorkerBridge.create_task` API.

The successor copies the failed task specification, keeps the original
objective, appends a root-cause note from the failed result, and sets:

- `parent_task_id` to the failed task ID
- `metadata.failure_successor` to `true`
- `metadata.successor_chain_depth` to the parent depth plus one
- a deterministic idempotency key of `failure-successor:<failed task ID>`

The watcher also checks existing children before creation. A live successor is
left alone, while a terminal successor still counts as the one allowed child.
The deterministic key makes concurrent or replayed creation safe at the public
API boundary.

## Configuration

Settings live under `worker_bridge.failure_successors` in gateway config:

```yaml
worker_bridge:
  failure_successors:
    enabled: true
    max_chain: 2
```

`enabled` defaults to `true`. `max_chain` defaults to `2` and is clamped to a
non-negative integer. A task whose current
`metadata.successor_chain_depth >= max_chain` does not produce another
successor.

## Exclusions

No successor is created for:

- cancelled tasks
- tasks below their configured auto-repair budget
- tasks at the maximum successor-chain depth
- tasks that already have a failure successor, including a live successor
- environmental timeouts reporting a silent worker, an unrecognized command,
  or an absent/missing `cmd.exe`
- all tasks when the feature is disabled

## Tests

`tests/gateway/test_failure_successors.py` covers one-successor creation,
maximum-chain stopping, cancelled tasks, replay idempotency, live successors,
environmental timeouts, disabled configuration, default configuration, and
tasks still in auto-repair.

Run:

```powershell
C:\Python314\python.exe -m pytest tests/gateway/test_failure_successors.py -q
```

## Rollback

Set `worker_bridge.failure_successors.enabled` to `false` to stop new successor
creation without changing existing tasks. Code rollback consists of removing
the failure-successor helpers and watcher call, then restoring the notifier
startup gate to depend only on `worker_bridge.gateway_alerts.enabled`. Existing
successor tasks remain ordinary bridge tasks and require no database migration.
