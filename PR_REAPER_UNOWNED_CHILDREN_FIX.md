# Upstream PR Description: Fix Kanban Worker Reaper Stealing Asyncio Child Processes

**Branch**: `contrib/reaper-unowned-children-fix`  
**Base**: `main` (`b39d76d902b0891457bc73a6eb43aa136f26d7a8`)  
**Related Issue/Defect**: Dispatcher silently hanging due to stolen child process exits in long-running gateway processes

---

## Problem
In `hermes_cli/kanban_db.py`, `reap_worker_zombies()` previously invoked `os.waitpid(-1, os.WNOHANG)` in a loop to collect any dead child process.

When Hermes runs as a long-lived gateway or desktop server, the process ALSO spawns subprocesses through `asyncio.create_subprocess_exec` and `_shell` (e.g. for tools, hooks, and background workers). Python's `asyncio` event loop monitors subprocess completion by installing child watchers that wait on specific child PIDs.

Calling `os.waitpid(-1, ...)` steals the exit status of asyncio's child processes before asyncio's watcher receives it. As a result:
1. `await proc.wait()` never resolves inside asyncio.
2. The coroutine awaiting the subprocess hangs indefinitely.
3. No exception or traceback is logged.
4. The dispatcher loop freezes permanently while the gateway appears healthy.

## Solution
1. Introduce process-local registration of explicitly spawned kanban worker PIDs (`_OWNED_WORKER_PIDS` protected by `_OWNED_WORKER_PIDS_LOCK`).
2. Hook `register_worker_pid(pid)` into `_set_worker_pid(conn, task_id, pid)`.
3. In `reap_worker_zombies()`, wait *only* on registered PIDs (`os.waitpid(pid, os.WNOHANG)`).
4. If a child PID is no longer valid or has already been reaped, remove it from the tracking set without widening the wait to `-1`.

## Tests Added
Four regression tests added in `tests/hermes_cli/test_reaper_does_not_steal_asyncio_children.py`:
- `test_reaper_ignores_children_it_does_not_own`: Verifies unowned zombie children are not reaped and their real owner can retrieve their exit code.
- `test_reaper_reaps_only_registered_workers`: Verifies registered worker PIDs are properly collected and cleared from tracking.
- `test_reaper_never_calls_waitpid_minus_one`: Enforces that `waitpid(-1)` is never invoked under any condition.
- `test_asyncio_child_still_resolves_after_a_reap`: End-to-end integration test confirming an active `asyncio` child process successfully resolves even when `reap_worker_zombies()` executes concurrently.
