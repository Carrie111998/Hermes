# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across a CLI, a
messaging gateway (Telegram, Discord, Slack, and ~20 other platforms), a TUI,
and an Electron desktop app. It learns across sessions (memory + skills),
delegates to subagents, runs scheduled jobs, and drives a real terminal and
browser. It is extended primarily through **plugins and skills**, not by
growing the core.

Two properties shape almost every design decision and are the lens for
reviewing any change:

- **Per-conversation prompt caching is sacred.** A long-lived conversation
  reuses a cached prefix every turn. Anything that mutates past context,
  swaps toolsets, or rebuilds the system prompt mid-conversation invalidates
  that cache and multiplies the user's cost. We do not do it (the one
  exception is context compression).
- **The core is a narrow waist; capability lives at the edges.** Every model
  tool we add is sent on every API call, so the bar for a new *core* tool is
  high. Most new capability should arrive as a CLI command + skill, a
  service-gated tool, or a plugin — not as core surface.

## Contribution Rubric

The full acceptance rubric, review cautions, and examples live in `website/docs/developer-guide/contribution-rubric.md`. Read it before proposing or reviewing a non-trivial change.

Core rules:

- Fix reproduced behavior on current `main`, and fix the bug class rather than one call site.
- Expand freely at product edges, but keep the core agent and model-tool schema narrow.
- Extend existing seams before adding managers, hooks, tools, or parallel infrastructure.
- Preserve prompt caching, strict message-role alternation, profile isolation, contributor credit, and real-path E2E coverage.
- User-facing behavioral configuration belongs in `config.yaml`; `.env` is for secrets.
- Third-party products ship as standalone plugins, not in the core tree.
- Verify both the reported premise and the original design intent before changing a load-bearing omission or restriction.

For new capability, use the least permanent footprint that works:

1. Extend existing code.
2. Add a CLI command plus skill.
3. Add a prerequisite-gated tool.
4. Ship a plugin.
5. Use an MCP server.
6. Add a core model tool only as a last resort.

Automated triage may close only confirmed `implemented_on_main`, `cannot_reproduce`, or `incoherent` cases. Taste and scope decisions stay with human maintainers.

---

## Development Environment

```bash
# Prefer .venv; fall back to venv if that's what your checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv` (for worktrees that share a venv with the
main checkout).

## Project Structure

Use the filesystem as the canonical map; counts and inventories go stale. Load-bearing entry points:

- `run_agent.py` / `agent/` — conversation loop and agent internals (`agent/AGENTS.md`).
- `cli.py` / `hermes_cli/` — classic CLI and subcommands (`hermes_cli/AGENTS.md`).
- `tools/` / `toolsets.py` — tool implementations and exposure (`tools/AGENTS.md`).
- `gateway/` — messaging runtime and platform adapters.
- `plugins/` — extension surfaces (`plugins/AGENTS.md`).
- `ui-tui/`, `web/`, `apps/desktop/` — three distinct UI surfaces with scoped guides.
- `skills/`, `optional-skills/` — bundled and opt-in skills with scoped guides.
- `cron/` and `plugins/kanban/` — durable scheduled and queued work.
- `tests/` — Python suite and test doctrine (`tests/AGENTS.md`).

---

## TypeScript Style

Applies to TypeScript across Hermes: desktop, TUI, website, and future TS packages.

- Prefer small nanostores over component state when state is shared, reused, or read by distant UI.
- Let each feature own its atoms. Chat state belongs near chat, shell state near shell, shared state in `src/store`.
- Components that render from an atom should use `useStore`. Non-rendering actions should read with `$atom.get()`.
- Do not pass state through three components when the leaf can subscribe to the atom.
- Keep persistence beside the atom that owns it.
- Keep route roots thin. They compose routes and shell; they should not become controllers.
- No monolithic hooks. A hook should own one narrow job.
- Prefer colocated action modules over hidden god hooks.
- If a callback is pure side effect, use the terse void form:
  `onState={st => void setGatewayState(st)}`.
- Async UI handlers should make intent explicit:
  `onClick={() => void save()}`.
- Prefer interfaces for public props and shared object shapes. Avoid `type X = { ... }` for object props.
- Extend React primitives for props: `React.ComponentProps<'button'>`, `React.ComponentProps<typeof Dialog>`, `Omit<...>`, `Pick<...>`.
- Table-driven beats condition ladders when mapping ids, routes, or views.
- `src/app` owns routes, pages, and page-specific components.
- `src/store` owns shared atoms.
- `src/lib` owns shared pure helpers.

## File Dependency Chain

```
tools/registry.py  (no deps — imported by all tool files)
       ↑
tools/*.py  (each calls registry.register() at import time)
       ↑
model_tools.py  (imports tools/registry + triggers tool discovery)
       ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

---

## Core Agent Architecture

Conversation-loop and curator guidance lives in `agent/AGENTS.md`; read it before editing `run_agent.py`, agent lifecycle, or curator behavior.

---

## CLI Architecture

Classic CLI, slash-command registry, configuration, skin, and profile guidance lives in `hermes_cli/AGENTS.md`; read it before changing those surfaces.

---

## UI Architecture Guides

UI instructions are scoped by surface. Read the relevant guide before editing it:

- `ui-tui/AGENTS.md` — Ink TUI process model, JSON-RPC flow, and commands.
- `web/AGENTS.md` — dashboard's PTY-backed embedded TUI contract.
- `apps/desktop/AGENTS.md` — Electron/Desktop authority, transport, state, and slash-command rules.

The three chat surfaces are intentionally distinct: classic CLI, dashboard-embedded TUI, and Electron Desktop. Do not rebuild one inside another.

---

## Tools and Toolsets

Tool footprint, registry, state-path, toolset, and delegation guidance lives in `tools/AGENTS.md`. A tool is not exposed merely because it is registered; its name must belong to an enabled toolset.

---

## Dependency Pinning Policy

All dependencies must have upper bounds to limit supply-chain attack surface.
This policy was established after the litellm compromise (PR #2796, #2810) and
reinforced after the Mini Shai-Hulud worm campaign (May 2026).

| Source type | Treatment | Example |
|---|---|---|
| PyPI package | `>=floor,<next_major` | `"httpx>=0.28.1,<1"` |
| Git URL | Commit SHA | `git+https://...@<40-char-sha>` |
| GitHub Actions | Commit SHA + comment | `uses: actions/checkout@<sha>  # v4` |
| CI-only pip | `==exact` | `pyyaml==6.0.2` |

**When adding a new dependency to `pyproject.toml`:**
1. Pin to `>=current_version,<next_major` for post-1.0 (e.g. `>=1.5.0,<2`).
2. For pre-1.0 packages, use `<0.(current_minor + 2)` (e.g. `>=0.29,<0.32`).
3. Never commit a bare `>=X.Y.Z` without a ceiling — CI and reviewers will reject it.
4. Run `uv lock` to regenerate `uv.lock` with hashes.

Reference: #2810 (bounds pass), #9801 (SHA pinning + audit CI).

---

## Configuration and Skins

Configuration and skin implementation guidance lives in `hermes_cli/AGENTS.md`. Global rule: use `config.yaml` for behavioral settings and reserve `.env` for credentials.

---

## Plugins

Detailed plugin architecture and contribution rules live in `plugins/AGENTS.md`; read it before editing that subtree. Keep the global footprint policy above in force: third-party products ship as standalone plugins, plugins do not special-case themselves into core, and generic seams must have a concrete consumer.

---

## Skills

Skill authoring and review standards are scoped to `skills/AGENTS.md` and `optional-skills/AGENTS.md`. Built-in skills belong in `skills/`; heavier or niche skills belong in `optional-skills/`.

---

## Cron (scheduled jobs)

Scheduler architecture, hardening invariants, and job-field guidance live in `cron/AGENTS.md`. Preserve role alternation by keeping deliveries in their own cron sessions.

---

## Kanban (multi-agent work queue)

Board, dispatcher, worker-tool, and isolation rules live in `plugins/kanban/AGENTS.md`. The board remains the hard isolation boundary; worker-only tools must not grow the ordinary-session schema.

---

## Important Policies

### Prompt Caching Must Not Break

Hermes-Agent ensures caching remains valid throughout a conversation. **Do NOT implement changes that would:**
- Alter past context mid-conversation
- Change toolsets mid-conversation
- Reload memories or rebuild system prompts mid-conversation

Cache-breaking forces dramatically higher costs. The ONLY time we alter context is during context compression.

Slash commands that mutate system-prompt state (skills, tools, memory, etc.)
must be **cache-aware**: default to deferred invalidation (change takes
effect next session), with an opt-in `--now` flag for immediate
invalidation. See `/skills install --now` for the canonical pattern.

### Background Process Notifications (Gateway)

When `terminal(background=true, notify_on_complete=true)` is used, the gateway runs a watcher that
detects process completion and triggers a new agent turn. Control verbosity of background process
messages with `display.background_process_notifications`
in config.yaml (or `HERMES_BACKGROUND_NOTIFICATIONS` env var):

- `all` — running-output updates + final message (default)
- `result` — only the final completion message
- `error` — only the final message when exit code != 0
- `off` — no watcher messages at all

---

## Profiles: Multi-Instance Support

Profiles are isolated `HERMES_HOME` instances. Apply the profile override before module imports, derive state from `get_hermes_home()`, and never cross profile boundaries implicitly. Full implementation and test guidance lives in `hermes_cli/AGENTS.md`.

---

## Known Pitfalls

### DO NOT hardcode `~/.hermes` paths
Use `get_hermes_home()` from `hermes_constants` for code paths. Use `display_hermes_home()`
for user-facing print/log messages. Hardcoding `~/.hermes` breaks profiles — each profile
has its own `HERMES_HOME` directory. This was the source of 5 bugs fixed in PR #3575.

### DO NOT introduce new `simple_term_menu` usage
Existing call sites in `hermes_cli/main.py` remain for legacy fallback only;
the preferred UI is curses (stdlib) because `simple_term_menu` has
ghost-duplication rendering bugs in tmux/iTerm2 with arrow keys. New
interactive menus must use `hermes_cli/curses_ui.py` — see
`hermes_cli/tools_config.py` for the canonical pattern.

### DO NOT use `\033[K` (ANSI erase-to-EOL) in spinner/display code
Leaks as literal `?[K` text under `prompt_toolkit`'s `patch_stdout`. Use space-padding: `f"\r{line}{' ' * pad}"`.

### `_last_resolved_tool_names` is a process-global in `model_tools.py`
`_run_single_child()` in `delegate_tool.py` saves and restores this global around subagent execution. If you add new code that reads this global, be aware it may be temporarily stale during child agent runs.

### DO NOT hardcode cross-tool references in schema descriptions
Tool schema descriptions must not mention tools from other toolsets by name (e.g., `browser_navigate` saying "prefer web_search"). Those tools may be unavailable (missing API keys, disabled toolset), causing the model to hallucinate calls to non-existent tools. If a cross-reference is needed, add it dynamically in `get_tool_definitions()` in `model_tools.py` — see the `browser_navigate` / `execute_code` post-processing blocks for the pattern.

### The gateway has TWO message guards — both must bypass approval/control commands
When an agent is running, messages pass through two sequential guards:
(1) **base adapter** (`gateway/platforms/base.py`) queues messages in
`_pending_messages` when `session_key in self._active_sessions`, and
(2) **gateway runner** (`gateway/run.py`) intercepts `/stop`, `/new`,
`/queue`, `/status`, `/approve`, `/deny` before they reach
`running_agent.interrupt()`. Any new command that must reach the runner
while the agent is blocked (e.g. approval prompts) MUST bypass BOTH
guards and be dispatched inline, not via `_process_message_background()`
(which races session lifecycle).

### Squash merges from stale branches silently revert recent fixes
Before squash-merging a PR, ensure the branch is up to date with `main`
(`git fetch origin main && git reset --hard origin/main` in the worktree,
then re-apply the PR's commits). A stale branch's version of an unrelated
file will silently overwrite recent fixes on main when squashed. Verify
with `git diff HEAD~1..HEAD` after merging — unexpected deletions are a
red flag.

### Don't wire in dead code without E2E validation
Unused code that was never shipped was dead for a reason. Before wiring an
unused module into a live code path, E2E test the real resolution chain
with actual imports (not mocks) against a temp `HERMES_HOME`.

### Tests must not write to `~/.hermes/`
The `_isolate_hermes_home` autouse fixture in `tests/conftest.py` redirects `HERMES_HOME` to a temp dir. Never hardcode `~/.hermes/` paths in tests.

**Profile tests**: When testing profile features, also mock `Path.home()` so that
`_get_profiles_root()` and `_get_default_hermes_home()` resolve within the temp dir.
Use the pattern from `tests/hermes_cli/test_profiles.py`:
```python
@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
```

---

## Testing

**Always use `scripts/run_tests.sh` for Python tests.** It enforces CI-parity isolation; direct pytest can inherit credentials, locale, timezone, or the real Hermes home.

```bash
scripts/run_tests.sh tests/path/test_file.py -k test_name
```

Global test rules:

- Test behavior and invariants, not volatile catalogs, enumeration counts, config-version literals, or source-text shapes.
- Put tests that assert about TypeScript/JavaScript artifacts in the JS/Vitest suite so the change classifier runs them.
- Treat pass-on-retry as a flaky bug, not a green result.
- Never let tests write to the real `~/.hermes/`.

Full runner, placement, flake, and anti-pattern guidance lives in `tests/AGENTS.md`.
