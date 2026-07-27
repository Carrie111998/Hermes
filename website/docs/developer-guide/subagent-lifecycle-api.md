---
title: Public Subagent Lifecycle API
sidebar_label: Subagent lifecycle API
---

# Public Subagent Lifecycle API

Plugins can launch and supervise fresh Hermes child sessions without importing
`tools.delegate_tool`, gateway internals, TUI state, or `AIAgent` fields.
The service resolves its parent from the current agent turn, so it works in
CLI, gateway, non-interactive, and kanban-worker sessions. Launching outside an
active agent turn fails closed with `No active Hermes parent session`.

```python
from agent.subagent_lifecycle import SubagentLaunchRequest

def launch_review(ctx):
    # Call from a plugin tool or hook while an agent turn is active.
    service = ctx.subagent_lifecycle
    handle = service.launch(SubagentLaunchRequest(
        goal="Review this change for regressions.",
        context="Only inspect the supplied repository.",
        role="leaf",
        correlation_id="review-42",
        allowed_toolsets=("file",),
    ))
    # Persist handle.to_dict() if desired.
    if service.wait(handle, timeout_seconds=2).timed_out:
        return handle.to_dict()
    return service.result(handle)
```

`SubagentHandle` is serializable and carries a versioned, opaque capability.
Pass it back to `status`, `wait`, `cancel`, `result`, or `reconnect`; malformed
or forged handles return `UNKNOWN`/`UNKNOWN_HANDLE` and cannot access a child.

The stable states are `PENDING`, `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`INTERRUPTED`, `CANCEL_REQUESTED`, `CANCELLED`, and `UNKNOWN`.

## Contract v2: private context, progress, and bounded relaunch

`service.contract_version` reports the exact public contract. Version 2 adds
three process-local plugin primitives without exposing the live parent or child
agent:

- `SubagentLaunchRequest(private_context=...)` attaches an opaque object to the
  child in memory. A plugin tool invoked by that child can read it only through
  `service.current_private_context()`. It is excluded from request repr/equality,
  handles, metadata, transcripts, results, and persistence, and is cleared before
  the terminal callback runs.
- `service.publish_progress(summary, priority=False)` relays one bounded summary
  through the child-owned host progress callback. Non-priority updates are limited
  to one every five seconds per child; priority updates may bypass that interval.
- `SubagentLaunchRequest(on_terminal=...)` receives `(handle, status, result)`
  once after terminal state is durable in memory. From that callback,
  `service.relaunch(handle, request)` may launch a fresh child under the same
  still-live parent. The replacement gets a new handle and correlation ID.

Private context and terminal callbacks are trusted plugin-code inputs, not model
tool arguments. Keep secrets out of public `context` and `metadata`; put only the
minimum process-local capability in `private_context`. Callback exceptions are
contained and do not change the terminal result. The callback and saved parent
reference are discarded after it returns.

`cancel(handle, reason=...)` is cooperative: it asks the child agent to
interrupt at its next safe boundary and returns `CANCEL_REQUESTED`; it never
claims completion until `wait` or `result` observes a terminal state. Terminal
results are immutable, idempotent, bounded to 32k characters, omit transcripts
and hidden reasoning, and include a stable result hash.

This API is lifecycle-managed asynchronous execution. Child construction and
completion use the same host-owned path as `delegate_task`, including parent
tool-resolution restoration, memory notification, serialized `subagent_stop`
hooks, resource cleanup, and child-cost rollup. It does not change the
synchronous `delegate_task` tool, batch delegation, or its gateway/TUI display.
The initial implementation retains metadata and terminal results in-process for
one hour.
After a process restart, `reconnect` returns `RECONNECT_UNAVAILABLE` and never
starts a replacement child. `relaunch` is deliberately in-process and must be
called from the terminal callback while its bounded parent reference is live.
Running Python threads and private contexts also cannot survive process exit;
callers must treat those handles as interrupted by process exit.

Requests are fail-closed: goal/context/metadata sizes are capped, unknown or
parent-broadening toolsets are rejected, and per-tool blocks, working-directory
overrides, and per-launch timeouts are explicitly rejected until Hermes can
support them without weakening isolation. Use `allowed_toolsets` to narrow a
child; Hermes's existing unsafe-tool block remains enforced.
