# Kanban Event Reference

Source: https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban#event-reference

## Lifecycle Events

| Kind | Payload | When |
|------|---------|------|
| `created` | `{assignee, status, parents, tenant}` | Task inserted |
| `promoted` | — | `todo → ready` because all parents hit `done` |
| `claimed` | `{lock, expires, run_id}` | Dispatcher atomically claimed a `ready` task |
| `completed` | `{result_len, summary?}` | Worker wrote result and task hit `done` |
| `blocked` | `{reason, kind, recurrences}` | Worker or human flipped task to `blocked` |
| `dependency_wait` | `{reason, kind}` | Worker blocked with `kind=dependency` |
| `block_loop_detected` | `{reason, kind, recurrences, limit}` | Task unblocked and re-blocked same reason N times |
| `unblocked` | — | `blocked → ready` (or `todo` if parents still open) |
| `archived` | — | Hidden from default board |

## Worker Telemetry Events

| Kind | Payload | When |
|------|---------|------|
| `spawned` | `{pid}` | Dispatcher started worker process |
| `heartbeat` | `{note?}` | Worker signaled liveness |
| `reclaimed` | `{stale_lock}` | Claim TTL expired |
| `crashed` | `{pid, claimer}` | Worker PID no longer alive |
| `timed_out` | `{pid, elapsed_seconds, limit_seconds, sigkill}` | `max_runtime_seconds` exceeded |
| `stale` | `{elapsed_seconds, ...}` | Task ran longer than stale timeout |
| `spawn_failed` | `{error, failures}` | Spawn attempt failed |
| `protocol_violation` | `{pid, claimer, exit_code, protocol_violation}` | Worker exited without kanban_complete/kanban_block |
| `gave_up` | `{failures, effective_limit, limit_source, error}` | Circuit breaker fired |

## Hook System

Three hooks fire in different processes:

| Hook | Fires in | When |
|------|----------|------|
| `kanban_task_claimed` | Dispatcher (gateway) | Right before worker subprocess spawns |
| `kanban_task_completed` | Worker process | When agent calls `kanban_complete` |
| `kanban_task_blocked` | Worker process | When agent calls `kanban_block` |

**Common kwargs:** `task_id`, `board`, `assignee`, `run_id`, `profile_name`
**kanban_task_completed adds:** `summary`
**kanban_task_blocked adds:** `reason`

## Workflow Loop Pattern

1. Implementer completes → card goes to `done`
2. Reviewer reviews → card goes to `blocked` (tests failed)
3. `kanban_task_blocked` hook fires in reviewer's worker process
4. Hook finds implementer's card via state file mapping
5. Hook appends failure report to implementer's card body
6. Hook resets implementer's card to `ready`
7. Dispatcher re-spawns implementer with failure report in context
8. Repeat until tests pass or max loops

## Pitfalls

- **Path.home() under HERMES_HOME:** When `HERMES_HOME` is set, `Path.home()` returns `<HERMES_HOME>/home` not the system home. Hooks must use `HERMES_WORKFLOW_FILES` env var or hardcoded fallback.
- **Hook runs in worker process:** The hook fires when the AGENT calls `kanban_complete`/`kanban_block`, not in the dispatcher. The worker is a separate subprocess with its own environment.
- **State file not updated by supervisor:** The supervisor subprocess may hang or crash silently (stdout/stderr DEVNULL). Hooks must read card status from kanban DB, not state file.
- **Hook auto-fire gap (2026-07-23):** Hooks work when called manually but may not auto-fire in worker subprocesses. The `invoke_hook` function fires through the plugin system, but the worker may load an older plugin version. Fix: clear `.pyc` files, restart gateway with `kill -USR1`. Manual test: `from plugins.workflow import _handle_workflow_node_event; _handle_workflow_node_event('task_id', 'done')`.
