# Hermes Agent — AI Contributor Instructions

Use this file as the always-on instruction layer for work in the Hermes Agent
repository. Keep it concise. Detailed architecture, subsystem notes, examples,
and rationale live in [`docs/development/agent-contributor-guide.md`](docs/development/agent-contributor-guide.md)
and in scoped `AGENTS.md` files nearer the code.

**Never give up on the right solution. Verify the premise and fix the whole bug
class rather than patching one symptom.**

## Product and architecture

Hermes is a personal AI agent shared across CLI, messaging gateways, TUI,
desktop, cron, and subagents. It is extended primarily through plugins and
skills rather than by growing the model-facing core.

Two rules shape nearly every change:

- **Prompt caching is sacred.** Do not mutate past context, swap toolsets, or
  rebuild the system prompt during a conversation. Context compression is the
  exception. State-changing slash commands should normally take effect next
  session, with an explicit opt-in for immediate invalidation.
- **Keep the core narrow.** Every core model tool is sent on every API call.
  Prefer: extend existing code → CLI command + skill → service-gated tool →
  plugin → catalogued MCP server → new core tool as a last resort.

## Contribution rules

- Reproduce reported behavior on current `main` before changing code.
- Trace the real runtime path and the original design intent. A missing link or
  restriction may be deliberate.
- Fix sibling call paths and the underlying bug class, not only the reported
  site.
- Extend existing abstractions instead of introducing parallel managers,
  hooks, or duplicated infrastructure.
- Large mechanical extractions from god-files are welcome when clearly scoped;
  speculative extension points without a real consumer are not.
- Product breadth belongs at the edges: platforms, providers, plugins, desktop,
  and TUI can grow without expanding the permanent model-tool schema.
- Preserve contributor authorship when building on external work.
- Do not add outbound telemetry or attribution without a user-facing opt-in.
- Third-party product integrations belong in standalone plugin repositories,
  not under the core `plugins/` tree.

## Configuration, paths, and profiles

- Behavioral settings go in `config.yaml`; `.env` is for credentials only.
  Do not add user-facing `HERMES_*` variables for non-secret settings.
- Use `get_hermes_home()` for state paths and `display_hermes_home()` for
  user-facing paths. Never hardcode `~/.hermes`.
- Profile selection is applied before imports. Profile operations themselves
  are HOME-anchored so every profile can list the shared profile directory.
- Gateway adapters using unique credentials must acquire and release scoped
  token locks.
- Tests must never read or write the user's real Hermes home.

## Tools, plugins, and schemas

- Do not add a new core tool when terminal, file tools, a skill, plugin, or MCP
  server already covers the use case.
- Tool modules register through `tools/registry.py`; handlers return JSON
  strings and need requirement checks when availability is conditional.
- Tool descriptions must not hardcode references to tools that may be absent.
  Add conditional cross-tool guidance in the shared definition resolver.
- Plugins stay self-contained. If a plugin needs a broader core hook, widen a
  generic interface rather than special-casing the plugin.
- Instruction-loading tools must not encourage lazy page-one-only reading.

## Conversation and gateway invariants

- Preserve strict message-role alternation. Never inject synthetic user
  messages mid-loop.
- Keep the system prompt byte-stable for a conversation.
- Commands that must work while an agent is blocked, including approvals and
  control commands, must bypass both the platform adapter queue and gateway
  runner guards.
- Background completion notifications must remain profile- and session-safe.
- Be cautious with process-global resolver state during child-agent execution.

## Testing and verification

Use the CI-parity wrapper, not direct `pytest`:

```bash
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # focused test
scripts/run_tests.sh -v --tb=long                     # extra pytest flags
```

Before reporting success:

1. Run the smallest meaningful focused test.
2. Run neighboring or integration tests for the affected path.
3. Exercise real imports, config propagation, I/O, and security boundaries
   against a temporary `HERMES_HOME` when they are part of the change.
4. Inspect the final diff for unrelated deletions or stale-branch reversions.

Testing rules:

- Assert behavior and invariants, not model lists, config-version literals,
  enumeration counts, or other data expected to change.
- Do not read source text in tests. Extract logic into a callable unit and
  execute it.
- JavaScript/TypeScript artifact assertions belong in the JS test suite, not
  Python tests that CI may not select for JS-only changes.
- A pass-on-retry is a flaky-test bug, not noise.
- Do not call a mocked unit test E2E.

## Known high-impact pitfalls

- Do not introduce new `simple_term_menu` usage; use the curses UI helpers.
- Do not use ANSI erase-to-EOL (`\033[K`) in prompt-toolkit display paths; use
  space padding.
- Do not hardcode cross-tool references in schemas.
- Do not wire previously unused code into live paths without real end-to-end
  validation.
- Before squash-merging a stale branch, update it against `main` and inspect
  the resulting diff for silent reversions.
- Preserve prompt caching, message alternation, profile isolation, and
  credential boundaries in every refactor.

## Scoped guidance

Read the closest scoped file when working in these areas:

- [`agent/AGENTS.md`](agent/AGENTS.md) — core agent, tools, delegation, context.
- [`hermes_cli/AGENTS.md`](hermes_cli/AGENTS.md) — CLI and configuration.
- [`plugins/AGENTS.md`](plugins/AGENTS.md) — plugin contracts and packaging.
- [`skills/AGENTS.md`](skills/AGENTS.md) — bundled and optional skills.
- [`cron/AGENTS.md`](cron/AGENTS.md) — scheduler behavior and durability.
- [`tests/AGENTS.md`](tests/AGENTS.md) — detailed test architecture and policy.
- [`ui-tui/AGENTS.md`](ui-tui/AGENTS.md) — TUI architecture and TypeScript UI.
- [`apps/desktop/AGENTS.md`](apps/desktop/AGENTS.md) — desktop-specific rules.

For contribution triage, architecture maps, profiles, curator, kanban, theme
internals, and full examples, consult the detailed contributor guide only when
the task requires them.