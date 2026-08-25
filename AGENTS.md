# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the hermes-agent codebase.

**Never give up on the right solution.**

This file is the permanent prefix of every session started here, so it stays
small on purpose: it holds only what must govern a whole session even after
compaction. Inventories, procedures, and subsystem detail live in
`website/docs/` and in the tree itself.
## What Hermes Is

Hermes is a personal AI agent that runs the same agent core across a CLI, a
messaging gateway (~20 platforms), a TUI, and an Electron desktop app. It
learns across sessions (memory + skills), delegates to subagents, runs
scheduled jobs, and drives a real terminal and browser. It is extended
primarily through **plugins and skills**, not by growing the core.

Two properties shape almost every design decision and are the lens for
reviewing any change:

- **Per-conversation prompt caching is sacred.** A long-lived conversation
  reuses a cached prefix every turn, so anything that mutates past context,
  swaps toolsets, or rebuilds the system prompt mid-conversation invalidates
  that cache and multiplies the user's cost. The one exception is context
  compression.
- **The core is a narrow waist; capability lives at the edges.** Every model
  tool is sent on every API call, so the bar for a new *core* tool is high.
## Contribution Rubric — What We Want / What We Don't

The project's intent layer, used two ways: by humans deciding what to build,
and by the automated triage sweeper deciding when a PR is safe to close on its
three allowed reasons (`implemented_on_main`, `cannot_reproduce`, `incoherent`)
and — just as important — **when NOT to close** one. Taste-based "out of scope"
closes are never automated. Hermes ships a **lot**, and the product surface
expands aggressively and on purpose; the restraint below aims at the **core
agent + the model tool schema**, the one place where every addition is paid for
on every API call.
### What we want

- **Fix real bugs, well.** Reproduce the symptom on current `main`, point to
  the exact line where it manifests, and fix the whole bug class, sibling call
  paths included.
- **Expand reach at the edges.** New platform adapters, channels, providers,
  models, and desktop/TUI features land routinely, including large ones, as
  long as they integrate with the existing setup/config UX (`hermes tools`,
  `hermes setup`, auto-install) instead of a raw env var.
- **Refactor god-files into clean modules.** Extracting a multi-thousand-line
  cluster out of `cli.py` / `run_agent.py` / `gateway/run.py` is wanted work
  even when the diff is huge: "every line traces to the request" applies to
  *feature* PRs, and a declared refactor's request IS the extraction.
- **Keep the core narrow** — see "The Footprint Ladder".
- **Extend, don't duplicate.** When several PRs integrate the same *category*,
  design one shared interface rather than merging them one at a time.
- **Behavior contracts over snapshots, and real-path E2E over green unit
  mocks** — see "Testing contract".
- **Cache-, alternation-, and invariant-safe** — see "Hard invariants".
- **Contributor credit preserved** — salvage external work by cherry-picking so
  authorship survives in git history.
### What we don't want (rejected even when well-built)

- **Speculative infrastructure** — hooks or extension points with no concrete
  consumer. A hook is not speculative if a contributor has a real, stated use
  case, even when that consumer ships separately.
- **New `HERMES_*` env vars for non-secret config** (see "Settings vs
  secrets"). Reject PRs telling users to "set X in your .env" unless X is a
  credential.
- **A new core tool when terminal + file already do the job, or when a skill
  would.** If the only barrier is file visibility on a remote backend, fix the
  mount.
- **Lazy-reading escape hatches on instructional tools** — no `offset`/`limit`
  on tools loading content the agent must read fully; models read page 1 and
  stop.
- **"Fixes" that destroy the feature they secure** — read the original
  commit's intent (`git log -p -S`) before restricting behavior.
- **Outbound telemetry or usage attribution without opt-in gating** — nothing
  until a generic user-facing opt-in (config gate + setup prompt +
  `hermes tools` toggle) exists.
- **Change-detector tests, cache-breaking mid-conversation, dead code wired in
  without E2E proof, and plugins that touch core files.** Plugins work within
  the ABCs and hooks we provide; if one needs more, widen the generic plugin
  surface rather than special-casing it in core (PR #5295 removed 95 lines of
  hardcoded plugin argparse from `hermes_cli/main.py`).
- **Third-party products in the core tree.** Observability backends, vendor
  SaaS connectors, analytics dashboards, and similar "someone else's product"
  plugins do not land under `plugins/` — they burden us with maintenance for a
  backend we don't own. Ship them as a standalone plugin repo users install
  into `~/.hermes/plugins/`.
### Before you call it a bug — verify the premise (and when NOT to close)

The most common reason a well-written PR gets closed is not code quality: the
change rests on a **wrong premise**, or treats an **intentional design as a
gap**. These tell a reviewer what to scrutinize and the sweeper when a PR is
NOT safe to close (when in doubt, leave it open for a human).

- **"Intentional design, not a gap."** Ask whether the isolation IS the design.
  Profiles are independent islands on purpose: a PR adding live config
  inheritance from the default profile was closed because coupling profiles is
  exactly what the design prevents. Read `git log -p -S "<symbol>"` before
  assuming something is unfinished.
- **"The premise doesn't hold against how X actually works."** Trace the real
  code first. If you cannot point to the exact line where the bug manifests AND
  show the fix changes that line's behavior, the premise is unverified — a real
  close: a usage-accumulation fix whose new branch **never executes** because an
  earlier guard already popped the state it depended on.
- **"The absence was deliberate."** Restoring "missing" `__init__.py` files
  made a test tree importable as a dotted package that shadowed the real plugin
  and deleted its `register()` at import time.
- **"Overreached, or resurrected an approach we had moved past."**

**Verify the claim AND the intent against the codebase before writing or
merging a fix.**
### The Footprint Ladder (new capability decision)

Each rung adds more permanent surface than the one above. Choose the highest
(least-footprint) rung that correctly solves the problem:

1. **Extend existing code** — a variation of something that already exists.
2. **CLI command + skill** — config/state/infra expressible as shell commands,
   with the agent running `hermes <subcommand>` guided by a skill. Zero
   model-tool footprint; the default for subscriptions, scheduled tasks, and
   service setup.
3. **Service-gated tool (`check_fn`)** — needs structured params/returns AND
   appears only when a prerequisite is configured.
4. **Plugin** — third-party, niche, or user-specific; lives in
   `~/.hermes/plugins/` or a pip package.
5. **MCP server in the catalog** — genuinely needs to be a tool but isn't
   core-fundamental. Zero permanent core-schema footprint.
6. **New core tool** — only when fundamental, broadly useful, and unreachable
   via terminal + file or MCP (terminal, read_file, web_search).

When 3+ open PRs try to integrate the same *category*, design an ABC +
orchestrator, wrap the existing built-in as the first provider, and turn the
competing PRs into plugins against it.
### Surface capability is a property of the SESSION, never of the process env

A tool that only works because of *who is on the other end of the connection* —
the desktop app's panes, the in-app browser, message reactions — must resolve
its availability from the **session's own source**, not from an env var on the
backend process. Client and backend are separate machines on separate
clocks: the desktop app may drive a backend spawned locally, over SSH, behind a
URL + token, or in Hermes Cloud, and only the first two carry
`HERMES_DESKTOP=1`. An env-keyed GUI gate is a silent no-op on the rest, and
the failure is invisible — the tool is stripped from the schema before the
model sees it.

- **The toolset is the surface gate.** Keep such tools off `_HERMES_CORE_TOOLS`
  and put them in a named toolset (`desktop_ui`, `project`); the GUI gateway's
  `_load_enabled_toolsets(platform)` folds it in when the session's platform
  says GUI. One resolver, every topology.
- **`check_fn` answers reachability or opt-in, not surface** — its results are
  TTL-cached process-wide, so a per-session answer cannot live there.
- **`HERMES_DESKTOP=1` means "spawned by the app"** — it gates the cron ticker
  and web-dist handling, not "a GUI is watching".

If the capability would still make sense with the client on another machine, it
is session-scoped. Cover it with a test asserting the GUI session gets the tool
**with the env var absent**.
## Development Environment

```bash
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv`, then `venv`, then
`$HOME/.hermes/hermes-agent/venv`, and is the only supported way to run
tests — see "Testing contract".
## Architecture entry points

Directory listings shift constantly; the filesystem is canonical. Full tree and
data flow: `website/docs/developer-guide/architecture.md`.

- `run_agent.py` — `AIAgent`, the core conversation loop.
- `model_tools.py` — tool orchestration, `handle_function_call()`.
- `toolsets.py` — toolset definitions and `_HERMES_CORE_TOOLS`.
- `cli.py` — `HermesCLI`, the interactive CLI orchestrator.
- `hermes_state.py` / `hermes_constants.py` — session store; profile-aware
  paths (`get_hermes_home()`, `display_hermes_home()`).
- `agent/` — provider adapters, memory, caching, compression, prompt assembly.
- `hermes_cli/` — subcommands, setup wizard, plugin loader, skin engine.
- `tools/` — implementations, auto-discovered via `tools/registry.py`.
- `gateway/` — messaging gateway; `ui-tui/` + `tui_gateway/` — Ink terminal UI
  and its JSON-RPC backend; `apps/desktop/` — Electron app (own `AGENTS.md`).

Imports flow one way: `tools/registry.py` (no deps) -> `tools/*.py` ->
`model_tools.py` -> `run_agent.py` / `cli.py`. No back-edge. User state lives
in `~/.hermes/` (`config.yaml`, `.env`, `logs/`), profile-aware via
`get_hermes_home()`. Deep-dives:
[agent loop](website/docs/developer-guide/agent-loop.md) ·
[prompt assembly](website/docs/developer-guide/prompt-assembly.md) ·
[gateway](website/docs/developer-guide/gateway-internals.md)
### Chat surfaces

Three surfaces, not interchangeable: the classic CLI, the Ink TUI
(`hermes --tui`, also embedded in `hermes dashboard` -> `/chat` over
`hermes_cli/pty_bridge.py`), and the Electron desktop app.

**Do not re-implement the primary chat experience in React.** The dashboard's
transcript, composer, and PTY-backed terminal belong to the embedded
`hermes --tui`; structured React *around* it (sidebars, inspectors, status
panels) is fine when it is not a second chat surface. The desktop app owns its
own composer, transcript, and slash-command pipeline — see
`apps/desktop/AGENTS.md`.
### TypeScript style

Applies everywhere TypeScript is used — desktop, TUI, website.

- Prefer small nanostores over component state when state is shared, reused, or
  read by distant UI. Each feature owns its atoms: shared state in `src/store`,
  routes in `src/app`, pure helpers in `src/lib`, persistence beside its atom.
- Render from an atom with `useStore`; read in non-rendering actions with
  `$atom.get()`. Don't thread state through three components when the leaf can
  subscribe.
- Keep route roots thin and hooks narrow; prefer interfaces for public props
  and table-driven maps over condition ladders.
## Extension routing

Two plugin surfaces live under `plugins/`, discovered alongside user plugins in
`~/.hermes/plugins/` and pip entry points.

- **General plugins** (`hermes_cli/plugins.py` + `plugins/<name>/`) expose
  `register(ctx)` and may add lifecycle hooks (pre/post tool call, pre/post LLM
  call, session start/end), tools, and CLI subcommands. **Discovery-timing
  pitfall:**
  `discover_plugins()` runs only as a side effect of importing
  `model_tools.py`, so a path reading plugin state without that import must
  call it explicitly (it is idempotent).
- **Typed plugin families** — memory, model, context-engine, image-gen
  providers — each have their own ABC + orchestrator and discovery.
  Memory-provider discovery is **bundled-first**, the reverse of the general
  later-wins order, so a dropped-in directory cannot shadow a shipped one, and
  the in-tree memory-provider set is **closed** (policy, May 2026).
- **Compatibility is a behavior contract, not a version literal** — canonical
  rules at
  `website/docs/developer-guide/plugins/index.md#native-plugin-compatibility-contract`.

Guides: [plugins](website/docs/developer-guide/plugins/index.md) ·
[memory](website/docs/developer-guide/memory-provider-plugin.md) ·
[model providers](website/docs/developer-guide/model-provider-plugin.md) ·
[platform adapters](website/docs/developer-guide/adding-platform-adapters.md)
### Adding a slash command

`hermes_cli/commands.py` holds one `COMMAND_REGISTRY` of `CommandDef` objects,
and every consumer derives from it — CLI and gateway dispatch, help, the
Telegram menu, Slack routing, autocomplete. Append a `CommandDef`, add the
handler branch in `HermesCLI.process_command()`, and — if the gateway serves
it — a branch in `gateway/run.py`. An *alias* needs only the `aliases` tuple.
## Adding New Tools

Most capabilities should NOT be core tools — walk the Footprint Ladder first.
For custom or local-only tools do **not** edit Hermes core: create
`~/.hermes/plugins/<name>/plugin.yaml` + `__init__.py` and register with
`ctx.register_tool(...)`.

A built-in core tool requires changes in **2 files**: `tools/your_tool.py`,
calling `registry.register(name=..., toolset=..., schema=..., handler=...,
check_fn=..., requires_env=[...])` at module level (auto-discovery imports any
`tools/*.py` with a top-level `register()` call); and `toolsets.py`, adding the
name to `_HERMES_CORE_TOOLS` or a named toolset. **The second step is
required** — discovery registers the schema, but a tool is only *exposed to an
agent* if its name appears in a toolset.

Handlers MUST return a JSON string. Schema descriptions mentioning paths use
`display_hermes_home()`; persistent state uses `get_hermes_home()`. Guide:
`website/docs/developer-guide/adding-tools.md`.
## Skills

`skills/` holds built-in skills loadable by default; `optional-skills/` holds
heavier or niche skills installed explicitly via `hermes skills install`. When
reviewing a skill PR, check which tree it targets — heavy-dependency or niche
skills belong in `optional-skills/`. Frontmatter and layout:
`website/docs/developer-guide/creating-skills.md`.
### Skill authoring standards (HARDLINE)

Every new or modernized skill — bundled, optional, or contributed — must meet
these before merge. Reviewers reject PRs that violate them.

1. **`description` <= 60 characters**, one sentence, ending in a period. State
   the capability, not the implementation; no marketing words.
2. **Tools named in SKILL.md prose must be native Hermes tools** (or MCP
   servers the skill expects), in backticks — never shell utilities the agent
   already wraps (`grep` -> `search_files`, `cat` -> `read_file`, `sed` ->
   `patch`). Name MCP dependencies under `## Prerequisites`.
3. **`platforms:` gating audited against actual script imports.** POSIX-only
   primitives (`fcntl`, `os.setsid`, `/proc`, hardcoded `/tmp`, `osascript`,
   `apt`, `systemctl`) must be declared; try cross-platform first and narrow
   only for a genuinely bound dependency.
4. **`author` credits the human contributor first** — replace a "Hermes Agent"
   author line with the contributor's real name.
5. **Modern section order** (title, a 2-3 sentence intro stating what it does
   and doesn't do, `## When to Use`, `## Prerequisites`, `## How to Run`,
   `## Quick Reference`, `## Procedure`, `## Pitfalls`, `## Verification`),
   ~200 lines for a complex skill and ~100 for a simple one.
6. **Scripts in `scripts/`, references in `references/`, templates in
   `templates/`,** cited by relative path from the skill directory.
7. **Tests at `tests/skills/test_<skill>_skill.py`** — stdlib + pytest +
   `unittest.mock` only, no live network.
8. **`.env.example` additions stay in one delimited block.**

Salvage checklist: the `hermes-agent-dev` skill,
`references/new-skill-pr-salvage.md`.
## Hard invariants

### Prompt caching must not break

Do **not** alter past context, change toolsets, or reload memories / rebuild
system prompts mid-conversation; the only sanctioned context mutation is
context compression. Slash commands that mutate system-prompt state (skills,
tools, memory) must be **cache-aware**: default to deferred invalidation
(effective next session) with an opt-in `--now` flag. `/skills install --now`
is the canonical pattern.

### Message-role alternation

Never two same-role messages in a row; never a synthetic user message injected
mid-loop. Cron deliveries are **not** mirrored into the target gateway
session — they land in their own cron session with a header/footer frame
precisely so the main conversation's alternation stays intact.

### Profiles: multi-instance support

Profiles are fully isolated instances, each with its own `HERMES_HOME`.
`_apply_profile_override()` in `hermes_cli/main.py` sets it before any module
import, so every `get_hermes_home()` reference scopes automatically. Profiles
are independent islands **on purpose**.

1. **Use `get_hermes_home()` for every HERMES_HOME path** (from
   `hermes_constants`); never `Path.home() / ".hermes"` in code that reads or
   writes state — that is what breaks profiles.
2. **Use `display_hermes_home()` in user-facing messages** — `~/.hermes` for
   the default, `~/.hermes/profiles/<name>` otherwise.
3. **Module-level constants are fine:** they cache `get_hermes_home()` at
   import time, after `_apply_profile_override()` ran.
4. **Tests that mock `Path.home()` must also set `HERMES_HOME`.**
5. **Adapters with a unique credential take a token lock**
   (`acquire_scoped_lock()` / `release_scoped_lock()` from `gateway.status`) so
   two profiles cannot use the same credential.
6. **Profile operations are HOME-anchored, not HERMES_HOME-anchored:**
   `_get_profiles_root()` returns `Path.home() / ".hermes" / "profiles"` on
   purpose, so `hermes -p coder profile list` sees every profile.
7. **Multiplex profile-scoped env reads MUST fail closed — never borrow from
   `os.environ`** (`agent/secret_scope.py`; #72348, #86905). Under
   `gateway.multiplex_profiles`, `os.environ` holds the **default profile's**
   values while a secondary profile's `.env` lives only in its secret scope, so
   every profile-level read — credentials *and* authorization — goes through
   `_get_scoped_secret()` or `gateway/authz_mixin.py`. A scoped miss returns
   the **default**; falling through to `os.environ` leaks another profile's
   value and silently breaks routing and admission. Only the unscoped
   default-profile path (`UnscopedSecretError`) and single-profile deployments
   read `os.environ`.
### Settings vs secrets

`.env` is **secrets only** — API keys, tokens, passwords; add one to
`OPTIONAL_ENV_VARS` in `hermes_cli/config.py` with its metadata. Everything
else (timeouts, thresholds, feature flags, paths, display preferences) belongs
in `config.yaml` under `DEFAULT_CONFIG`. If internal code needs an env mirror
for back-compat, bridge it from `config.yaml` in code (see `terminal.cwd` ->
`TERMINAL_CWD`). Bump `_config_version` **only** to actively migrate existing
user config; adding a key to an existing section is handled by the deep-merge.

**Three config loaders — know which one you are in:** `load_cli_config()`
(`cli.py`), `load_config()` (`hermes_cli/config.py`, most subcommands), and a
direct YAML read (`gateway/run.py` + `gateway/config.py`). If the CLI sees your
new key and the gateway does not, you are on the wrong one.
### Working directory and session isolation

The CLI uses the process's current directory; messaging uses `terminal.cwd`
from `config.yaml`, which the gateway bridges to `TERMINAL_CWD` for child
tools. `MESSAGING_CWD` is **removed** and `TERMINAL_CWD` in `.env` is
deprecated. Background `delegate_task` is detached from the current turn but
still process-local — work that must survive a process restart uses `cronjob`
or `terminal(background=True, notify_on_complete=True)`.
### Dependency pinning

Every dependency needs an upper bound (established after the litellm compromise,
reinforced after the Mini Shai-Hulud worm campaign). PyPI packages pin
`>=floor,<next_major`; pre-1.0 uses `<0.(minor + 2)`. Git URLs and GitHub
Actions pin a commit SHA; CI-only pip installs pin `==exact`. A bare `>=X.Y.Z`
is rejected. Run `uv lock` after touching `pyproject.toml`.
### Update pipeline (`hermes update`)

Transactional in shape:
`plan -> snapshot -> apply -> restart-per-kind -> verify -> report`. Every
stage exists because its absence was a real field failure, so a PR weakening
one answers for the failure class it guards. Three rules are load-bearing:
the pre-update **snapshot covers every profile with an identical file set**
(a partial set creates torn-restore states); **restart is fleet-wide and
drain-first**, because restarting
only the invoking profile's service leaves siblings on stale `sys.modules`;
and **verify compares each live gateway's stamped `code_sha`** against the
fresh checkout, since automation must never treat a mixed-version fleet as
healthy. `hermes serve` dies with the desktop app by design while the
messaging gateway (`gateway run`) **survives** it — do not "fix"
gateway-dies-with-app reports by re-parenting it, or update locks by widening
the tree-kill.
### Streaming delivery contract (stream-is-the-message adapters)

Adapters with `draft_stream_is_message = True` keep ONE cumulative native
stream per turn; the stream IS the final message. Four invariants, each from a
live duplicate-final incident: draft frames are **prefix-stable**; the
**consumer declares the final** via `finish(final_text)`; **interim sends set
`metadata["_interim_send"] = True`** at both egress doors; and a final beside a
sealed stream **reconciles by `edit_message`, never a plain send**. Enforced by
`tests/gateway/test_stream_final_contract.py` — read it before touching a
streaming adapter.
### Known pitfalls

- **Do not infer process identity from argv substrings** — the bug class behind
  ~10 fleet-update issues. Use the canonical matchers
  (`gateway.status.looks_like_gateway_command_line`,
  `hermes_cli.update_cmd._hermes_holder_subcommand`), derive flag sets from the
  parser, match FULL cmdlines, and never blanket-exclude scan ancestors.
- **The gateway has TWO message guards; a control command must bypass both** —
  the base adapter's pending-message queue and the runner's `/stop` `/new`
  `/queue` `/status` `/approve` `/deny` interception — and dispatch inline, not
  via `_process_message_background()`.
- **Do not hardcode cross-tool references in schema descriptions** — add them
  dynamically in `get_tool_definitions()`, or the model hallucinates calls when
  the other toolset is disabled.
- **All CLI menu-pickers MUST use curses** (`hermes_cli/curses_ui.py`), and
  never `\033[K` in spinner/display code — space-pad instead.
- **`_last_resolved_tool_names` is a process-global** saved and restored around
  subagent execution, so it may read stale.
- **Squash merges from stale branches silently revert recent fixes** — update
  against `main` first, then check `git diff HEAD~1..HEAD`.
## Testing contract

**ALWAYS use `scripts/run_tests.sh`** — never call `pytest` directly. It gives
hermetic CI parity (credential env vars unset, `TZ=UTC`, `LANG=C.UTF-8`,
per-file subprocess isolation, no xdist); direct `pytest` on a many-core
machine with API keys set diverges from CI in both directions. Each file runs
in a fresh subprocess, so module-level state cannot leak between files. A
failing FILE is retried once in a fresh subprocess; pass-on-retry is green but
prints a FLAKY summary, and FLAKY is a bug to fix (loose wall-clock bounds
>= 2s, event-based sync).

```bash
scripts/run_tests.sh                                    # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                     # one directory
scripts/run_tests.sh tests/agent/test_foo.py -k test_x  # file + -k
```

- **Tests must not write to `~/.hermes/`.** `tests/conftest.py`'s
  `_isolate_hermes_home` autouse fixture redirects `HERMES_HOME` to a temp dir;
  profile tests must also mock `Path.home()` so `_get_profiles_root()` resolves
  inside it — pattern: `tests/hermes_cli/test_profiles.py`.
- **Exercise the real path.** Anything touching resolution chains, config
  propagation, security boundaries, remote backends, or file/network I/O — and
  any previously-dead module being wired in — is tested with real imports,
  never mocks, against a temp `HERMES_HOME`.
- **Place tests where CI will run them.** `scripts/ci/classify_changes.py`
  gates jobs on changed files, so a Python test asserting about `package.json`,
  `tsconfig.json`, or `.ts` sources never runs on a PR that only touches those
  files — those assertions belong in the JS (vitest) suite.
- **Don't fake the host OS.** Mark per-host behavior
  `@pytest.mark.linux_only` / `macos_only` / `windows_only`; pure functions
  taking a platform as data stay unmarked. **If the test needs the interpreter
  to believe it is on another OS in order to pass, it belongs on that OS.**
- **Use the marker, never a bare `skipif`.**
  `scripts/ci/list_os_marked_tests.py` greps the marker *name* to choose what
  the macOS/Windows lanes import, then filters with `-m <marker>`, so a
  `skipif(sys.platform != "win32")` test skips on Linux and is never imported
  on the Windows lane — it runs nowhere. A file-local marker alias is listed
  but fully deselected: green over zero coverage.
- **Live Windows process topology:** `windows-venv-e2e.yml` runs
  `test_venv_holder_windows_live.py` on a real runner (pushes to `wine2e/**`
  only) for claims mocks cannot reproduce. Assert against the gateway ANCESTOR
  found by argv — the venv shim makes every spawn a launcher chain.
- **Don't write change-detector tests.** A test that fails whenever data
  *expected* to change is updated (model catalogs, config version literals,
  enumeration counts) adds no coverage and breaks CI on routine updates.
  `DEFAULT_CONFIG["_config_version"] == 21` is the antipattern; the invariant
  form is `raw["_config_version"] == DEFAULT_CONFIG["_config_version"]`.
- **Never read source code in tests.** Reading a `.py`/`.ts` file's text tests
  the *shape of the source*, not behavior, and is banned outright: it passes
  when a call site exists but is wired wrong, and fails on a
  behavior-preserving refactor. Extract the logic into a small pure or
  DI-testable function and call it for real.
## Scoped context

Nested `AGENTS.md` files are discovered progressively as the agent enters a
directory and are appended to tool results, which compaction can summarize
away — anything that must govern a whole session belongs in this file. Each
nested file stays under 8,000 characters (target 6,000). Today there is one:
`apps/desktop/AGENTS.md` (with `apps/desktop/DESIGN.md`), scoping the Electron
desktop app.

Run `python scripts/check_context_file_limits.py --json .` before adding or
growing a context file.
