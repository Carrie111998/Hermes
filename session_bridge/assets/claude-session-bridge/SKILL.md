---
name: session-bridge
description: Browse and continue the unified Claude, Codex, and Hermes session catalog.
user-invocable: true
disable-model-invocation: true
---

# Unified session catalog

Use `/resume`, then `Ctrl+A`, for Claude Code's native picker. Use
`/session-bridge` for the global catalog spanning Claude, Codex, and Hermes.

1. Call `session_bridge:session_search` with the user's query. Use an empty query
   to browse recent sessions. Display provider, title, cwd, last activity, mirror
   state, and a short preview for each result.
2. Ask the user to make an explicit session selection. Do not continue a session
   based only on a search ranking or inferred intent.
3. Call `session_bridge:session_get` for the selected session and summarize its
   bridge relationship and available native mirrors.
4. If continuation is requested, confirm the selected target provider, then call
   `session_bridge:session_continue` with the selected session ID and target.

The catalog tools are authenticated by the configured `session_bridge` MCP
server. Never create a native session through this skill; native visibility is
managed by the local registrar.
