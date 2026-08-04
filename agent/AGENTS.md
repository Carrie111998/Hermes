# Agent Core Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns rules for the
conversation loop and modules under `agent/`.

## Conversation invariants

- A conversation's system prompt and tool schemas are byte-stable after the
  session starts. Do not reload memories, rebuild prompts, or swap toolsets
  mid-conversation; context compression is the only context rewrite.
- Slash commands that change prompt inputs defer to the next session by
  default. An explicit `--now` path may invalidate the current cache.
- Preserve strict message-role alternation. Never inject a synthetic user
  message in the middle of the loop.
- Resolution chains and state boundaries need real-import tests against a
  temporary `HERMES_HOME`; unit mocks alone hide wiring failures.

## Agent loop

`run_agent.py` owns `AIAgent` and the synchronous tool-calling loop.
`model_tools.py` owns tool orchestration. `agent/` contains extracted provider,
memory, caching, compression, and lifecycle modules.

Do not infer `AIAgent`'s constructor from examples: its live signature is
authoritative and changes frequently. Preserve the shared iteration budget,
interrupt checks, one-turn grace call, and assistant/tool message pairing.

`model_tools._last_resolved_tool_names` is process-global. Delegated children
save and restore it around execution; new readers must tolerate that boundary.

## Delegation

`tools/delegate_tool.py` creates isolated child sessions:

- `role="leaf"` is focused and cannot delegate recursively.
- `role="orchestrator"` may delegate when
  `delegation.orchestrator_enabled` permits it and
  `delegation.max_spawn_depth` has not been reached.
- Batch tasks run concurrently up to
  `delegation.max_concurrent_children`.
- Background delegation is process-local. Work that must survive a restart
  belongs in cron or a durable external queue.

Keep child timeouts, iteration limits, MCP inheritance, and auto-approval
controlled by the existing `delegation:` configuration.

## Curator

`agent/curator.py` and `agent/curator_backup.py` maintain agent-created skills.
The hard invariants are:

- bundled and hub-installed skills are never curator targets;
- pinned skills are exempt from automatic transitions;
- automatic maintenance archives but never deletes;
- delete refuses pinned skills, while normal editing remains possible;
- usage state remains in the profile's Hermes home.

User-facing behavior belongs in
[`website/docs/user-guide/features/curator.md`](../website/docs/user-guide/features/curator.md).

## Profile-safe state

Use `get_hermes_home()` for state paths and `display_hermes_home()` for
user-facing paths. Never hardcode `~/.hermes`.

Profile discovery itself is intentionally HOME-anchored:
`_get_profiles_root()` uses `Path.home() / ".hermes" / "profiles"` so any
active profile can enumerate its siblings.

Module-level paths may cache `get_hermes_home()` only because
`_apply_profile_override()` runs before imports. Tests that patch `Path.home()`
must also set `HERMES_HOME`.

## Dead-code rule

Do not wire an unused module into a live path without an end-to-end test of the
actual import and resolution chain. Dead code may encode an abandoned or unsafe
approach; read its history before reviving it.
