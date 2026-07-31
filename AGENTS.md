# Hermes Agent Development Guide

Instructions for AI coding assistants and developers working on Hermes Agent.

**Never give up on the right solution.**

## Product invariants

Hermes runs one agent core across the CLI, messaging gateway, TUI, desktop,
scheduled jobs, and plugins.

- **Prompt caching is sacred.** A conversation keeps a byte-stable system
  prompt and toolset. Do not mutate past context or rebuild prompt inputs
  mid-session except through context compression.
- **Core is a narrow waist.** Capability grows at the edges. Every core tool
  schema is paid for on every model call, so new core tools are rare.
- **Message alternation is strict.** Never create adjacent same-role messages
  or inject a synthetic user turn inside the tool loop.
- **State has one authority.** Profile, session, gateway, and UI state stay
  with the layer that can be correct about them.

## Contribution intent

Hermes is expansive at the edges and conservative at the waist.

We want:

- fixes tied to a current-main reproduction and the actual failing line;
- whole-class repairs across sibling call paths;
- new adapters, channels, providers, models, and UI capability built on
  existing setup/configuration surfaces;
- declared refactors that split god-files into focused modules;
- extensions of existing abstractions before new managers or hooks;
- behavioral invariants and real-path integration proof;
- contributor authorship preserved when salvaging external work.

We do not want:

- speculative hooks without a real consumer;
- non-secret behavior configured through new `HERMES_*` environment variables;
- core tools when terminal, file, a skill, plugin, or MCP server suffices;
- pagination on instructional content the agent must read completely;
- security fixes that destroy the feature they protect;
- telemetry or third-party attribution without generic user opt-in;
- in-tree third-party-product plugins or new memory-provider integrations;
- plugin-specific branches in core files;
- change-detector or source-regex tests.

Automated PR triage may close only for `implemented_on_main`,
`cannot_reproduce`, or `incoherent`. Taste, scope, and "won't implement"
decisions stay human-owned.

## Verify premise and intent first

Before calling something a bug:

1. reproduce it on current `main`;
2. trace the runtime path to the exact line;
3. inspect the original intent with `git log -p -S`;
4. confirm an earlier guard does not make the proposed branch unreachable;
5. preserve load-bearing omissions and isolation boundaries.

When the premise is uncertain, ask rather than ship a fix that fights the
design. Never resurrect abandoned code without real-import end-to-end proof.

## Footprint ladder

Choose the first rung that solves the problem:

1. extend existing code;
2. CLI command plus skill;
3. service-gated tool with `check_fn`;
4. standalone plugin;
5. MCP server in the catalog;
6. new core tool.

When several contributions target one category, design one generic interface
and provider orchestration instead of merging parallel special cases.

## Policy routing

Root rules always apply. Before editing a scoped area, read its owner:

| Scope | Policy owner |
|---|---|
| contribution setup, workflow, Windows portability | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| agent loop, caching, delegation, curator, profile state | [`agent/AGENTS.md`](agent/AGENTS.md) |
| CLI commands, config loaders, skins, terminal UI | [`hermes_cli/AGENTS.md`](hermes_cli/AGENTS.md) |
| gateway adapters, control guards, notifications | [`gateway/AGENTS.md`](gateway/AGENTS.md) |
| tool registry, schemas, toolsets | [`tools/AGENTS.md`](tools/AGENTS.md) |
| plugins and provider discovery | [`plugins/AGENTS.md`](plugins/AGENTS.md) |
| bundled and optional skill authoring | [`skills/AGENTS.md`](skills/AGENTS.md) |
| Ink TUI and dashboard PTY surface | [`ui-tui/AGENTS.md`](ui-tui/AGENTS.md) |
| Electron desktop | [`apps/desktop/AGENTS.md`](apps/desktop/AGENTS.md) |
| cron scheduler | [`cron/AGENTS.md`](cron/AGENTS.md) |
| Kanban dispatcher and worker protocol | [`plugins/kanban/AGENTS.md`](plugins/kanban/AGENTS.md) |
| Python test placement and proof | [`tests/AGENTS.md`](tests/AGENTS.md) |

User-facing behavior remains documented under `website/docs/`; the scoped
policies above own code-change invariants.

## Development workflow

Follow [`CONTRIBUTING.md`](CONTRIBUTING.md) for installation and environment
setup. Prefer `.venv`, then `venv`; `scripts/run_tests.sh` also knows the
managed Hermes install.

- Read the live implementation rather than trusting file counts or example
  signatures.
- Extend existing code paths before creating new files or abstractions.
- Keep each change traceable to the request.
- Run the tests selected by the scoped policy and then `git diff --check`.
- Keep credentials out of logs, fixtures, and committed examples.

The filesystem is the project-structure source of truth. Important boundaries
are `run_agent.py`/`agent/`, `model_tools.py`/`tools/`, `cli.py`/`hermes_cli/`,
`gateway/`, `cron/`, `plugins/`, `ui-tui/`, `apps/desktop/`, and `tests/`.

## Configuration and state

- User settings live in `config.yaml`; `.env` contains credentials only.
- Use `get_hermes_home()` for state paths and `display_hermes_home()` for
  user-facing paths. Never hardcode `~/.hermes`.
- Apply profile overrides before importing modules that cache Hermes-home paths.
- Preserve existing configuration precedence and validate each loader or
  consumer a change crosses.

## Dependency policy

All dependencies have upper bounds:

| Source | Pin |
|---|---|
| PyPI, post-1.0 | `>=floor,<next-major` |
| PyPI, pre-1.0 | bounded compatible minor range |
| Git URL | immutable commit SHA |
| GitHub Action | immutable commit SHA plus version comment |
| CI-only pip tool | exact version |

Regenerate `uv.lock` after Python dependency changes. A bare lower bound is
not acceptable.

## Cross-platform baseline

- Probe external commands with `shutil.which()` and provide a native fallback.
- Prefer `psutil` to POSIX-only liveness probes.
- Guard `termios`, `fcntl`, process groups, signals, and filesystem encodings.
- Do not assume `/proc`, `/tmp`, bash, or GNU utilities.
- Keep platform-specific code behind a small boundary with tests for both
  supported and fallback behavior.

## TypeScript baseline

Applies to desktop, TUI, website, and future TypeScript packages:

- Shared state lives in small feature-owned stores; components subscribe at
  the narrowest useful boundary.
- Route roots compose; they do not become controllers.
- Hooks own one job. Prefer colocated action modules over god hooks.
- Side-effect callbacks make `void` intent explicit.
- Public object shapes prefer interfaces and primitive-derived props.
- Table-driven mappings beat repeated condition ladders.
- `src/app` owns routes/pages, `src/store` shared atoms, and `src/lib` shared
  pure helpers.

Desktop-specific authority, state, and performance rules live in its scoped
policy.

## Merge and authorship discipline

Preserve contributor commits where practical. Before a squash merge, update
the branch from current main using a non-destructive, reviewable workflow and
inspect the resulting diff for unrelated reversions. A stale branch must never
silently restore an older version of an unrelated file.

Do not merge based on green checks alone when the proof is stale or does not
exercise the changed boundary.
