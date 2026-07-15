# Desktop Agent Initialization Timeout — Implementation Plan

**Goal:** Prevent false desktop agent-initialization timeouts caused by Windows
status probes and websocket backpressure.

**Architecture:** Keep the dashboard asyncio loop free by executing the local PID
probe in its default executor. Decouple agent usability from optional transport
and enrichment work by setting the readiness event immediately after callback
wiring and treating later failures as post-ready warnings.

**Tech stack:** Python, FastAPI/asyncio, threaded TUI gateway, pytest through the
repository test wrapper.

## Task 1: Establish a clean baseline

Run the existing focused tests from the isolated worktree:

```bash
scripts/run_tests.sh tests/hermes_cli/test_web_server_boot_handshake.py
scripts/run_tests.sh tests/test_tui_gateway_server.py::test_start_agent_build_passes_session_model_override
```

## Task 2: Add the event-loop regression

**Files:**

- Modify: `tests/hermes_cli/test_web_server_boot_handshake.py`

Add a test that blocks the first `get_running_pid()` call while concurrently
requesting `/api/version`. Assert the version response arrives before the PID
probe is released. Run that test alone and confirm it fails against the current
synchronous implementation.

## Task 3: Add the readiness-boundary regression

**Files:**

- Modify: `tests/test_tui_gateway_server.py`

Add a test that blocks the `session.info` emit after constructing a fake agent.
Assert `agent_ready` is set and the agent is attached before releasing the emit.
Run that test alone and confirm it fails against the current end-of-build signal.

## Task 4: Implement the status-loop fix

**Files:**

- Modify: `hermes_cli/web_server.py`

Resolve the selected `get_running_pid` call with
`asyncio.get_running_loop().run_in_executor`. Preserve the explicit profile PID
path behavior. Re-run the event-loop regression.

## Task 5: Implement the readiness fix

**Files:**

- Modify: `tui_gateway/server.py`

Set `agent_ready` after `_wire_callbacks`. Track that the essential boundary was
crossed so later exceptions are logged as post-ready setup failures instead of
setting `agent_error`. Preserve the final idempotent signal for failure paths.
Re-run the readiness regression and the existing model-override build test.

## Task 6: Verify and review

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_web_server_boot_handshake.py
scripts/run_tests.sh tests/test_tui_gateway_server.py
scripts/run_tests.sh tests/gateway/test_status.py
```

Review the final diff for protocol changes, cleanup races, and accidental edits.
Record the diagnosis and verified fix in the Hermes shared-memory wing.

