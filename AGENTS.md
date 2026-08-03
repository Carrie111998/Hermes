# Hermes Agent Development Guide

Hermes Agent is a multi-provider AI agent with CLI, gateway, tools, skills, memory, plugins, and scheduled/background execution. Favor narrow, composable changes that preserve user control and work across providers and platforms.

## Progressive Disclosure

This file contains only project-wide rules and orientation. The full maintainer reference moved to [`docs/agent-guides/development-guide.md`](docs/agent-guides/development-guide.md).

Before changing a subsystem, search that reference for the relevant heading and read only that section. It covers:

- agent loop, CLI, TUI, dashboard, and desktop architecture
- tools, configuration, dependencies, skills, plugins, and toolsets
- delegation, curator, cron, and kanban
- profiles, gateway behavior, known pitfalls, and detailed testing guidance

Do not load the full reference by default. Prefer code, schemas, tests, and targeted sections over carrying the entire guide in context.

## Contribution Standard

Good changes are:

- provider- and model-agnostic unless capability differences require a narrow adapter
- platform-neutral across CLI and configured gateways
- opt-in when they add recurring cost, background work, or external side effects
- compatible with existing config and profile layouts
- built on existing registries and extension points instead of parallel infrastructure
- covered by behavior tests and real execution evidence

Reject changes that add provider lock-in, duplicate an existing subsystem, silently expand cost or permissions, weaken approval boundaries, or introduce a large dependency for a small convenience.

Use this footprint ladder for new capabilities:

1. Prompt or skill guidance
2. Existing tool composition
3. Optional plugin or deferred tool
4. Core tool or runtime change only when the lower layers cannot satisfy the need

## Development Setup

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

Prefer `.venv`; use the checkout's existing environment when present. Do not install into the system Python.

## Project Orientation

- `agent/`: prompt assembly, model loop, context, and runtime behavior
- `hermes_cli/`: CLI, config, commands, setup, and diagnostics
- `tools/`: tool implementations, schemas, registry, approvals, and toolsets
- `gateway/`: messaging adapters, routing, session context, and delivery
- `cron/`: scheduled execution
- `plugins/`: optional extension surfaces
- `skills/`: bundled procedural skills
- `tests/`: behavior and integration tests mirroring the owning subsystem
- `apps/desktop/`: desktop client, with its own `AGENTS.md`

Inspect the implementation and nearby tests before editing. Never guess file contents, dependency availability, config paths, or runtime behavior.

## Hard Invariants

### Prompt and context

- Preserve prompt-prefix caching. Stable content must remain byte-stable and precede volatile session data.
- Avoid duplicated guidance across the system prompt, tool schemas, skills, and project context. Keep one authoritative home per rule.
- Prefer expressive schemas and progressive disclosure to long examples or model-specific scaffolding.
- Treat current Claude, Codex, and Grok generations as frontier peers by default. Add model-family exceptions only from observed evidence.
- Keep security, approval, privacy, production, and destructive-action boundaries explicit.

### Paths, config, and profiles

- Never hardcode `~/.hermes`. Resolve paths through the shared Hermes-home/profile helpers.
- Keep secrets in environment or secret stores, not `config.yaml`, logs, prompts, tests, or fixtures.
- Respect profile isolation. A process may serve multiple profiles; avoid process-global state that can leak configuration or tools across them.
- When adding config, update the authoritative schema/default path and every loader that owns that setting. Do not create a fourth config path.

### Tools and approvals

- Register tools through the shared registry and canonical toolsets.
- Do not hardcode references to optional tools in schemas that may load without them.
- External writes, destructive commands, credential changes, and cross-profile mutations must retain their approval and scope checks.
- The gateway has separate message and approval/control paths. Changes must preserve both.
- Background bounded work must surface completion; long-lived servers/watchers must remain trackable and avoid shell-level daemonization.

### Compatibility

- Preserve public CLI flags, config keys, tool schemas, and stored-state migrations unless the change explicitly includes a compatibility path.
- Do not introduce new `simple_term_menu` usage.
- Do not use `\033[K` in spinner or display code.
- Treat `_last_resolved_tool_names` in `model_tools.py` as process-global and unsafe for per-session decisions.
- Never merge stale branches that can revert newer fixes. Rebase or recreate from current `origin/main` and inspect the final diff.

## Testing Rules

- Write behavior tests before production changes when practical: RED, GREEN, REFACTOR.
- Put tests beside the owning subsystem under `tests/`; reuse existing fixtures and isolation helpers.
- Tests must not read or write the real Hermes home. Use `tmp_path` and monkeypatched `HERMES_HOME`.
- Test durable behavior and invariants, not catalog counts, source text, internal constants, or implementation trivia.
- Avoid tests that inspect source code as text. Exercise imports, schemas, functions, CLI output, and runtime behavior instead.
- Run targeted tests while iterating, then the broader owning suite. Run the full suite when the change crosses subsystem boundaries.
- Report the exact commands and real results. Do not claim success from static inspection alone.

## Change Checklist

Before declaring a change complete:

1. Confirm the user-visible behavior and compatibility boundary.
2. Inspect relevant code, tests, and the targeted maintainer-reference section.
3. Add or update behavior tests.
4. Exercise the real path.
5. Check prompt, tool-schema, dependency, permission, and background-work footprint.
6. Review the final diff for unrelated changes and stale-branch regressions.
7. Run targeted verification and the appropriate broader suite.
8. Update concise documentation only where behavior is non-obvious.
