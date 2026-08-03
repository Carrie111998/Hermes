# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the
`hermes-agent` codebase.

**Never give up on the right solution.**

<!-- The startup context floor is 20,000 characters. Keep this file below
18,000 characters so headings/wrappers and future edits retain margin. -->

## What Hermes Is

Hermes runs the same personal-agent core across a CLI, messaging gateway, TUI,
and Electron desktop app. It learns through memory and skills, delegates work,
runs scheduled jobs, and drives real terminal and browser tools. Capability is
extended primarily through plugins and skills, not by growing the core.

Two properties govern almost every change:

- **Per-conversation prompt caching is sacred.** Past context, toolsets, and the
  system-prompt prefix remain byte-stable for a conversation; compression is the
  only established context-rewrite boundary.
- **The core is a narrow waist; capability lives at the edges.** Product reach
  should grow, but permanent agent-loop and model-tool-schema surface should not
  grow when an existing extension path can solve the problem.

## Contribution Contract

This is the normative project intent. Detailed rationale and examples live in
[`docs/development/contribution-rubric.md`](docs/development/contribution-rubric.md).
Automated triage may close only `implemented_on_main`, `cannot_reproduce`, or
`incoherent` cases. Taste-based “won't implement” decisions stay with a human;
when uncertain, leave the contribution open.

### Aim at

- **Real bug fixes.** Reproduce on current `main`, identify the exact runtime
  line, and fix the whole class through the shared path, including siblings.
- **Breadth at the edges.** Platforms, channels, providers, models, and UI
  surfaces are welcome when they integrate through existing setup/config UX.
- **Declared god-file refactors.** Focused extraction from `cli.py`,
  `run_agent.py`, or `gateway/run.py` is valid even when mechanically large.
- **The narrowest working footprint.** New model tools are the expensive last
  resort because every active schema is paid on every model call.
- **Extension rather than duplication.** Reuse existing managers, hooks, and
  interfaces. When several real consumers establish one category, design a
  shared ABC/orchestrator instead of merging parallel one-offs.
- **Behavior contracts.** Test relationships and user-visible behavior, not
  snapshots of catalogs, version literals, counts, or source-text shape.
- **Real-path validation.** Config propagation, security boundaries, remote
  backends, and file/network I/O require actual imports and a temp
  `HERMES_HOME`; mocks alone do not prove the integration.
- **Cache-, alternation-, and invariant-safe changes.** Never emit two adjacent
  same-role messages or inject a synthetic user message mid-loop.
- **Contributor credit.** Salvage external work by cherry-picking/rebase-merging
  so authorship survives; do not reimplement salvageable work from scratch.

### Reject even when polished

- Speculative hooks, callbacks, or extension points with no concrete consumer.
  A stated real consumer makes an extension non-speculative.
- User-facing `HERMES_*` variables for non-secret behavior. Secrets belong in
  `.env`; timeouts, thresholds, feature flags, paths, and preferences belong in
  `config.yaml`.
- A core model tool when existing terminal/file capability, a CLI command plus
  skill, a service-gated tool, plugin, or MCP server is sufficient.
- `offset`/`limit` escape hatches on instructional loaders such as skills,
  prompts, and playbooks; the model must receive those documents completely.
- Security fixes that destroy the protected feature. Read the original intent
  before restricting behavior and preserve the useful path.
- Outbound telemetry, attribution, or third-party identifiers without a generic
  user opt-in gate exposed through config/setup/tooling.
- Cache-breaking mid-conversation behavior, dead code wired without E2E proof,
  change-detector tests, source-text tests, or plugins that special-case
  themselves in core files.
- New in-tree integrations for someone else's SaaS/product. Ship them as
  standalone plugins under `~/.hermes/plugins/` or a pip entry point. Existing
  integrations may be fixed, but they are not precedent for adding more.

### Verify the premise before fixing it

- Check whether the apparent limitation is intentional isolation. Read history
  (`git log -p -S`) before coupling profiles, components, or subsystems.
- Trace runtime reachability. A branch that cannot execute cannot explain the
  report; point to the line where the symptom manifests and where the fix acts.
- Treat omissions as potentially load-bearing (for example, package markers can
  change import/shadowing behavior).
- Do not overreach or resurrect a direction maintainers intentionally replaced.
  Keep the PR to the agreed base and offer extras separately.

### Footprint ladder

Stop at the first rung that correctly solves the capability:

1. Extend existing code.
2. CLI command + skill.
3. Service-gated tool (`check_fn`).
4. Plugin.
5. MCP server in the catalog.
6. New core model tool, only when fundamental and unavailable through the above.

When three or more contributions target one provider category, build one shared
ABC/orchestrator, wrap the built-in as its first provider, and adapt competing
work to that interface.

## Context Budget and Required Reading

`agent/prompt_builder.py` uses an explicit `context_file_max_chars` when set;
otherwise it derives a model-window budget with a 20,000-character fallback and
floor. Oversized files are head/tail truncated. Root rules therefore stay here;
subsystem detail is loaded on demand.

Read the listed reference **before editing or reviewing that area**:

| Area | Required reference |
|---|---|
| Contribution design, triage, or policy | `docs/development/contribution-rubric.md` |
| `run_agent.py`, `cli.py`, `model_tools.py`, `toolsets.py`, slash commands | `docs/development/architecture-core.md` |
| `tools/delegate_tool.py`, subagent orchestration | `docs/development/architecture-core.md` |
| `ui-tui/`, `tui_gateway/`, dashboard chat | `docs/development/architecture-tui.md` |
| `apps/desktop/` | `apps/desktop/AGENTS.md` and `apps/desktop/DESIGN.md` |
| Gateway lifecycle or platform adapters | `website/docs/developer-guide/gateway-internals.md` and `gateway/platforms/ADDING_A_PLATFORM.md` |
| `plugins/**` or plugin discovery, hooks, and providers | `docs/development/plugins.md` |
| Bundled or optional skills | `docs/development/skills-authoring.md` |
| CLI skins/themes | `docs/development/skins.md` |
| Curator, cron, or kanban | `docs/development/subsystems.md` |
| Config, `.env`, loaders, working directories | `docs/development/configuration.md` |
| Tests, CI isolation, or test quality | `docs/development/testing.md` |
| A new built-in model tool | `website/docs/developer-guide/adding-tools.md` |

Root rules remain authoritative. Each listed reference owns its local details
and procedures; if wording conflicts, root wins. Do not duplicate this contract.
Keep startup context files below 18,000 characters and progressively loaded
subdirectory context files below 8,000; move explanations, inventories, and
tutorials to non-auto-loaded docs.

## Development Environment

```bash
# Prefer .venv; fall back to venv if that is what the checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` also probes `$HOME/.hermes/hermes-agent/venv`, allowing
worktrees to share the managed checkout's environment.

Load-bearing root entry points are `run_agent.py` (conversation loop),
`model_tools.py` (tool orchestration), `toolsets.py` (tool exposure), `cli.py`
(interactive CLI), and `hermes_state.py` (session store). Inspect the filesystem
and real definitions/usages rather than trusting a static inventory.

## TypeScript Style

Applies to desktop, TUI, website, and future TypeScript packages.

- Put shared/reused/distant UI state in small feature-owned nanostores; renderers
  use `useStore`, while non-rendering actions read with `$atom.get()`.
- Keep persistence beside its owning atom and declare whether the key is global,
  profile-, connection-, project-, session-, or window-scoped.
- Do not prop-drill through three components when the leaf can subscribe.
- Keep route roots thin. Prefer narrow hooks and colocated action modules over
  controller pages or monolithic hooks.
- Make side-effect intent explicit: `onState={st => void setState(st)}` and
  `onClick={() => void save()}`.
- Prefer interfaces for public props/object shapes and extend React primitives
  with `React.ComponentProps`, `Omit`, or `Pick`.
- Prefer table-driven mappings over condition ladders.
- `src/app` owns routes/pages, `src/store` shared atoms, and `src/lib` shared
  pure helpers.

## Global Engineering Contracts

### Prompt and conversation state

- Never alter past messages, toolsets, memories, or the system-prompt prefix in
  a live conversation. Slash commands that mutate prompt state default to next
  session; an explicit `--now` path may use the established invalidation flow.
- Maintain strict role alternation. Do not manufacture user turns to steer the
  loop.
- Preserve the feature while hardening it, and prove resolution chains with real
  imports instead of wiring dormant code from unit mocks.

### Configuration

- Behavioral settings go in `config.yaml`; only keys/tokens/passwords go in
  `.env`. Adding a key is deep-merged and does not require a config-version bump;
  bump only for an active migration/shape change.

### Profiles (Multi-Instance Support)

- Use `get_hermes_home()` for active-profile state and
  `display_hermes_home()` in user-facing paths. Never hardcode `~/.hermes` or
  `Path.home() / ".hermes"` for profile-scoped state.
- Module-level `get_hermes_home()` values are safe because profile override runs
  before imports. Profile discovery itself remains HOME-anchored so any active
  profile can list all profiles.
- Tests that mock `Path.home()` must also set `HERMES_HOME`. Platform adapters
  using unique credentials must acquire/release scoped token locks.

### Tools and plugins

- Built-in tool modules register with `tools.registry`, return JSON strings, and
  must also be exposed through `_HERMES_CORE_TOOLS` or another toolset. Use
  `check_fn` for optional prerequisites.
- `TOOLSETS` in `toolsets.py` is the single built-in registry;
  `_HERMES_CORE_TOOLS` supplies inherited defaults. Configure platform choices
  through `hermes tools`/`platform_toolsets`; `agent.disabled_toolsets` is the
  final global suppression layer.
- Tool schema paths use `display_hermes_home()`; persistent tool state uses
  `get_hermes_home()`.
- Do not hardcode another tool's name in a schema when that toolset may be
  disabled. Add conditional cross-references while assembling definitions.
- Plugins stay behind generic hooks/ABCs and do not modify core files for
  plugin-specific behavior. New memory backends and third-party products ship
  as standalone plugin repositories.

### Dependencies

All dependencies have upper bounds: PyPI packages use
`>=floor,<next-major` (pre-1.0: `<0.(current minor + 2)`), git dependencies use a
full commit SHA, GitHub Actions use a SHA plus version comment, and CI-only pip
installs use exact pins. Run `uv lock` after `pyproject.toml` changes.

### Testing and evidence

- Always run Python tests through `scripts/run_tests.sh`; it strips credentials,
  sets hermetic HOME/TZ/locale, and runs each file in a fresh subprocess.
- Test behavior/invariants, not expected-to-change data or regexes over source
  files. Put JS-artifact tests in Vitest, not Python.
- File/network/config/security/remote changes require real-path coverage against
  a temp `HERMES_HOME`. Tests never write to the user's real home.
- Before claiming completion, run the focused suite and inspect the final diff.
  See `docs/development/testing.md` for runner and flake policy.

## Known Pitfalls

- **No new `simple_term_menu`.** Legacy call sites remain, but new interactive
  menus use `hermes_cli/curses_ui.py`.
- **No `\033[K` in spinner/display code.** It leaks through
  `prompt_toolkit.patch_stdout`; clear with space padding.
- **`_last_resolved_tool_names` is process-global.** Delegate execution saves and
  restores it; code reading it during child execution may observe stale state.
- **Gateway controls cross two guards.** Approval/control commands must bypass
  both the base adapter's active-session queue and `gateway/run.py` dispatch,
  inline rather than through `_process_message_background()`.
- **Tracked background work needs notification.** Use
  `terminal(background=True, notify_on_complete=True)` for bounded work; gateway
  verbosity is controlled by `display.background_process_notifications`.
- **Stale squash merges can revert unrelated fixes.** Recut on current `main`,
  preserve contributor commits, and inspect the resulting merge diff.
- **Missing files may be intentional.** In particular, adding `__init__.py` can
  make a test directory shadow the real plugin package.
- **Do not wire dead code without E2E validation.** Reachability through the real
  CLI/gateway/provider/tool path is the proof.
