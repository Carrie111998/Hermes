# Session Env Vars Gap

## Problem

When `workflow_start` is called, the tool handler runs in a subprocess that doesn't inherit the gateway's session context. The session env vars (`HERMES_SESSION_PLATFORM`, `HERMES_SESSION_CHAT_ID`, `HERMES_SESSION_THREAD_ID`, `HERMES_SESSION_PROFILE`) are not available to the tool handler.

This means `_subscribe_final_cards()` in the engine cannot create a subscription because `get_session_env()` returns empty strings.

## Impact

- The final-layer card's completion notification goes to the gateway's default adapter instead of the calling session
- No wake injection into the calling agent's session
- The caller doesn't get notified when the workflow completes

## Root cause

The `workflow_start` tool handler is registered by the workflow plugin and called through the gateway's tool dispatch system. But the tool handler runs in a worker subprocess that doesn't have access to the gateway's ContextVars where session info is stored.

The `get_session_env()` function reads from gateway ContextVars, which are only available in the gateway process, not in worker subprocesses.

## Current workaround

None — the notification goes to the gateway's default adapter. The caller must manually check workflow status.

## Fix needed

The gateway's tool dispatch system needs to pass session env vars to tool handlers. This could be done by:
1. Setting the env vars in the subprocess before calling the tool handler
2. Or passing session info as part of the tool call context
3. Or having the engine read session info from the gateway's ContextVars directly (requires the tool to be called from the gateway process, not a subprocess)
