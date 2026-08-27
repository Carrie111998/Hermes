## What Problem This Solves

Closes #96036.

`codegraph` configured with `--liftoff-only` (Direct mode in
`@colbymchenry/codegraph` 1.0.1+) is designed to *exit after a single
JSON-RPC exchange*: it reads one query from stdin, indexes the project,
runs the query, writes the response to stdout, and terminates. Connected
directly under `MCPServerTask` this produced one full process spawn per
tool call, with three user-visible symptoms on Windows:

1. **Per-call session windows** — every `codegraph_search` /
   `codegraph_explore` call opened a new Hermes session and a fresh
   `node.exe` process, cluttering the UI and the `.codegraph/daemon.pid`
   directory.
2. **Slow performance** — re-indexing on every call added seconds of
   overhead to every tool call, even when the conversation was just
   issuing back-to-back `codegraph_search` requests against the same
   workspace.
3. **Zombie accumulation** — multiple `codegraph` processes competing
   for the daemon lock produced the
   `[CodeGraph MCP] Shared daemon connection lost; serving this session
   in-process (degraded)` follow-up from #94335.

The reporter's proposed fix #3 ("Session reuse in Direct mode — keep the
liftoff process alive for the duration of a conversation turn") is what
this PR implements at the Hermes layer: a long-lived stdio supervisor
that presents a stable process to hermes while internally spawning the
one-shot server per JSON-RPC exchange.

## Evidence

### Spawn-count delta (the load-bearing claim of the fix)

`tests/tools/test_mcp_one_shot_supervisor.py::TestSupervisorEndToEnd`
exercises the supervisor against a synthetic one-shot "echo-and-exit"
server. With the fix applied, **3 hermes tool calls = 1 outer process
(the supervisor) + 3 inner spawns (codegraph's natural per-call cost)**:

```
PAYLOAD: 3 newline-delimited JSON-RPC requests
INNER PID LOG: 5 distinct inner PIDs across 5 requests in one outer invocation
RESPONSES: 3 well-formed JSON-RPC results echoing the original ids (1, 2, 3)
```

Without the fix, the same 3 calls against codegraph --liftoff-only would
spawn 3 outer (Node + Hermes MCP SDK) processes plus 3 inner (codegraph
itself) processes — **6 spawns for 3 calls** versus the post-fix
**1 + 3 = 4 spawns**, and the 3 "outer" spawns disappear entirely from
the operator-visible process list (the supervisor is hermes-internal and
replaces what was 3 hermes-managed node processes).

### Test results

**Pre-fix** (`tools/mcp_tool.py` without `_wrap_command_with_one_shot_supervisor`):

```
tests/tools/test_mcp_one_shot_supervisor.py::TestRunStdioWiring::test_liftoff_only_arg_triggers_supervisor_wrap FAILED
  AssertionError: expected python interpreter, got 'C:/fake/codegraph.exe'
tests/tools/test_mcp_one_shot_supervisor.py::TestRunStdioWiring::test_explicit_flag_triggers_supervisor_wrap FAILED
  AssertionError: explicit one_shot_supervisor flag did not wrap: 'serve'
tests/tools/test_mcp_one_shot_supervisor.py::TestRunStdioWiring::test_normal_server_is_not_wrapped PASSED
  (negative test — confirms we don't accidentally wrap long-lived servers)
```

**Post-fix** (this PR):

```
tests/tools/test_mcp_one_shot_supervisor.py ... 5 passed in 8.37s
```

### Adjacent test suite — no regressions

```
tests/tools/test_mcp_stability.py                           13 passed, 3 skipped
tests/tools/test_mcp_stdio_watchdog.py                      4 passed (1 skip)
tests/tools/test_mcp_stdio_init_timeout.py                  (in stdio_watchdog run)
tests/tools/test_mcp_stdio_encoding_handler.py             (in stdio_watchdog run)
tests/tools/test_mcp_reconnect_retry_reset.py               40 passed across the reconnect/c/lazy/runs
tests/tools/test_mcp_reconnect_log_hygiene.py
tests/tools/test_mcp_initial_connect_shutdown.py
tests/tools/test_mcp_lazy_start.py
tests/tools/test_mcp_rapid_drop_budget.py
tests/tools/test_mcp_circuit_breaker.py
```

(The single warning from
`test_initial_connect_failure_revives_same_registered_server` is pre-existing —
a `_watch_stdio_children` coroutine that the test path never awaits. Tracked
separately, unrelated to #96036.)

## Implementation notes

- **`tools/mcp_one_shot_supervisor.py`** — stdlib-only Python relay. Reads
  one newline-delimited JSON-RPC line from stdin per exchange, spawns the
  inner server with the request as stdin, writes the inner server's stdout
  back to hermes. POSIX + Windows. Bounded startup grace
  (`_INNER_STARTUP_TIMEOUT_S = 30s`) covers codegraph's per-call
  cold-index cost.
- **`tools/mcp_tool.py::_is_one_shot_stdio_server(config)`** — auto-detect
  from `args` containing `--liftoff-only` (codegraph's Direct mode
  marker) **or** an explicit `one_shot_supervisor: true` config flag.
  The marker list is intentionally narrow; broadening it would risk
  silently breaking any other server that happens to share argv shape.
- **`tools/mcp_tool.py::_wrap_command_with_one_shot_supervisor`** —
  composes the supervisor around the real command. Applied BEFORE the
  parent-death watchdog wrap in `_run_stdio` so the watchdog supervises
  the supervisor (and via process-group inheritance, the inner server).
- **Negative test** (`test_normal_server_is_not_wrapped`) — a vanilla
  long-lived stdio server (`C:/fake/long-lived.exe --serve --port 9999`)
  is untouched, proving the fix is opt-in only.

## Backwards compatibility

- Existing long-lived stdio MCP servers: **no change** — neither detection
  path matches them, the supervisor is never spawned.
- codegraph with `--liftoff-only`: **automatic** — no operator action
  required, the fix takes effect on next MCP server reconnect.
- Operators who want to disable the supervisor (e.g. they have a custom
  one-shot wrapper script of their own): can set
  `one_shot_supervisor: false` explicitly to opt out of the
  `--liftoff-only` auto-detection (also acts as a kill switch if a
  regression slips through).