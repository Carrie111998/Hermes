# Desktop Agent Initialization Timeout — Design

## Problem

The desktop can report `Hermes error - agent initialization timed out` even after
the `AIAgent` and slash worker have been constructed. Two independent pieces of
backpressure currently combine to produce the false failure:

1. `/api/status` calls `get_running_pid()` synchronously on the asyncio event
   loop. On Windows that can run `tasklist` for seconds, starving websocket
   flushes and every other dashboard request.
2. `_start_agent_build()` does not set `agent_ready` until after optional
   post-build work, including the synchronous `session.info` websocket emit.
   A congested transport therefore defines whether initialization succeeded.

## Approved approach

Apply two narrow concurrency fixes rather than increasing the 30-second timeout.

### Keep blocking PID liveness work off the event loop

Resolve `get_running_pid()` through the loop's default executor inside
`/api/status`, preserving the existing local-path selection and return shape.
The endpoint already uses this pattern for remote health and restart-drain
resolution.

### Signal readiness at the usable-agent boundary

Treat these steps as essential initialization:

- construct and attach the agent;
- establish the baseline model state;
- best-effort attach the slash worker and approval notifier;
- wire the agent callbacks.

Set `agent_ready` immediately after that boundary. Credits hydration,
notification polling, session-boundary hooks, `session.info` emission, and MCP
late refresh remain post-ready enrichment. A failure in enrichment is logged but
must not retroactively set `agent_error` for an already usable agent.

The existing `finally: ready.set()` remains as the failure-path guarantee so
waiters receive `agent_error` instead of hanging when essential construction
fails.

## Regression coverage

1. Patch the local PID probe to block and make a concurrent fast API request.
   The fast request must complete while the PID probe is still blocked.
2. Patch `session.info` emission to block. `agent_ready` must be set while that
   emit remains blocked and the attached agent must be usable.

## Non-goals

- Changing the public status payload or desktop protocol.
- Increasing initialization or websocket timeouts.
- Replacing Windows process detection globally.
- Making optional post-ready enrichment asynchronous in this change.

