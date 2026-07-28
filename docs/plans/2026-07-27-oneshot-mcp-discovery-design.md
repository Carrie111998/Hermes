# One-Shot MCP Discovery Design

## Problem

The top-level Hermes CLI starts MCP discovery in a background thread before
dispatching `hermes -z`, but the one-shot runtime constructs `AIAgent`
immediately. `AIAgent` snapshots the tool registry during construction. When
the background discovery thread has not registered the MCP tools yet, that
one-shot session permanently omits them.

The race is visible across otherwise equivalent profiles: a personal run can
contain a real `mcp_browser_control_execute` call while a North Labs run started
at the same time contains no MCP tools. Passing `-t browser-control` changes
startup timing and happens to avoid the race, but it is not a safe contract.

## Approved Approach

Make the one-shot runtime own its MCP readiness contract:

1. Idempotently start background MCP discovery from `_run_agent`.
2. Perform Hermes's existing bounded discovery wait before constructing
   `AIAgent`.
3. Leave the top-level startup call in place; the helper is process-idempotent.
4. Preserve the configured `mcp_discovery_timeout`, so a dead MCP server cannot
   make one-shot mode hang indefinitely.

This keeps discovery asynchronous for normal startup while ensuring the first
one-shot tool snapshot is not taken before the bounded readiness gate.

## Verification

- A unit regression test records discovery-start, discovery-wait, and agent
  construction order.
- The existing one-shot test module remains green.
- Live profile canaries run without `--toolsets` and pass only when the exported
  Hermes transcript contains a real `mcp_browser_control_execute` tool call.
- Personal, Co-Intelligence, North Labs, AC Foods, and ARX are checked
  separately.

## Non-Goals

- Making every MCP connection synchronous.
- Requiring callers or prompts to name `browser-control`.
- Treating an agent's textual `BROKER_OK` response as proof of tool use.
- Changing browser-control tenant authorization or Kasm profile semantics.
