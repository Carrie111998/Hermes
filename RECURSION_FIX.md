# Failure-successor recursion fix

## Root cause

The failure-successor policy and its watcher orchestration did not have a
one-way ownership boundary. In the faulty live path, the symbol named
`create_failure_successors` could resolve back to watcher orchestration, while
`_worker_bridge_failure_successors` invoked that symbol. The two entries then
called each other before either pass completed. Idempotency could not stop the
cycle because recursion occurred before successor creation returned.

The recovered checkout showed the same ambiguity structurally: failure-policy
tests imported `create_failure_successors` from `worker_bridge_watchers`, and
there was no standalone `gateway/failure_successors.py` policy module. The fix
gives the helper and watcher method distinct names and responsibilities:

- `gateway.failure_successors.create_failure_successors` is synchronous policy
  code. It only reads and writes through the supplied bridge and never imports
  or invokes the watcher.
- `GatewayWorkerBridgeWatchersMixin._worker_bridge_failure_successors` owns
  scheduling and has a per-instance boolean re-entrancy guard protected by
  `try/finally`.
- The notifier loop calls the guarded pass exactly once per watcher tick,
  independently of whether gateway alerts are enabled.

## Call graph

Before (live failure):

```text
_worker_bridge_notifier_watcher
  -> _worker_bridge_failure_successors
    -> create_failure_successors
      -> _worker_bridge_failure_successors
        -> create_failure_successors
          -> ... until RecursionError / runaway successor activity
```

After:

```text
_worker_bridge_notifier_watcher (one call per tick)
  -> _worker_bridge_failure_successors (per-instance re-entry guard)
    -> asyncio.to_thread(create_failure_successor_tasks)
      -> gateway.failure_successors.create_failure_successors
        -> WorkerBridge.list_tasks
        -> WorkerBridge.create_task (zero or one child per failed parent)

recursive/concurrent entry
  -> _worker_bridge_failure_successors
    -> return 0 without entering policy code
```

## Tests

`tests/gateway/test_failure_successors.py` now verifies:

1. A complete notifier watcher tick with one failed task invokes successor
   policy exactly once and creates one successor.
2. A recursive call triggered while policy code is still running returns zero
   and does not invoke policy a second time.
3. An exception releases the guard, allowing the next pass to run normally.
4. Maximum-chain enforcement and replay idempotency still hold through the
   watcher method.

The existing pure-policy cases still cover cancellation, environmental
timeouts, auto-repair exhaustion, disabled configuration, live/existing
children, default settings, chain bounds, and replay idempotency. The review
continuation suite remains green.

Verification command:

```powershell
C:\Python314\python.exe -m pytest tests/gateway/test_failure_successors.py tests/gateway/test_review_continuation.py -q
```

Expected result: `22 passed`.

Adjacent watcher/wiring verification:

```powershell
C:\Python314\python.exe -m pytest tests/gateway/test_worker_bridge_watchers.py tests/gateway/test_worker_bridge_watcher_wiring.py -q
```

Expected result: `13 passed`.

## Expected live log

Normal creation produces one line per eligible failed parent:

```text
worker-bridge failure successor created for <task_id> at chain depth <depth>
```

If a nested or overlapping call is attempted while a pass is active, it is a
single no-op warning:

```text
worker-bridge failure successor pass already running; recursive entry ignored
```

There should be no repeating alternation between
`create_failure_successors` and `_worker_bridge_failure_successors`, no repeated
`RecursionError` traceback, and no duplicate child for the same failed parent.
If policy code raises, the watcher records its ordinary single
`worker-bridge alerts tick failed` exception and the next tick can retry.

## Rollback

Runtime rollback: set
`worker_bridge.failure_successors.enabled: false` in `config.yaml`. The watcher
reloads this setting each tick, so no gateway restart is required to stop new
failure successors.

Code rollback: revert `gateway/failure_successors.py`, the guarded watcher
method/call in `gateway/worker_bridge_watchers.py`, and the associated tests.
Existing successor tasks are ordinary worker-bridge tasks; rollback does not
delete or mutate them and requires no database migration.

No gateway restart was performed as part of this fix.
