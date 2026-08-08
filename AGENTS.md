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

## Contribution Rubric — What We Want / What We Don't

This is the project's intent layer. Use it two ways:

1. **For humans and for your own work** — what gets merged and what gets
   rejected, so a contribution aims at the target.
2. **For automated review (the triage sweeper)** — guidance on when a PR is
   safe to close on the three allowed reasons (`implemented_on_main`,
   `cannot_reproduce`, `incoherent`) and, just as important, **when NOT to
   close** one. Taste-based "we don't want this / out of scope" closes are NOT
   an automated decision — those stay with a human maintainer. The sweeper's
   job here is to recognize design intent and *avoid wrongly closing a
   legitimate contribution*, not to make the won't-implement call itself.

Read the balance right: Hermes ships a **lot** — most merges are bug fixes to
real reported behavior, and the product surface (platforms, channels,
providers, models, desktop/TUI features) expands aggressively and on purpose.
The restraint below is aimed squarely at the **core agent + the model tool
schema**, the one place where every addition is paid for on every API call.
"Smallest footprint" governs *how a capability is wired into the core*, NOT
whether the product is allowed to grow. We are expansive at the edges and
conservative at the waist.

### What we want

- **Fix real bugs, well.** The bulk of what lands is `fix(...)` against an
  actual reported symptom. A good fix reproduces the symptom on current
  `main`, points to the exact line where it manifests, and fixes the whole bug
  class — sibling call paths included — not just the one site the reporter hit.
- **Expand reach at the edges.** New platform adapters, channels, providers,
  models, and desktop/TUI/dashboard features are welcome and land routinely,
  including large ones (a new messaging channel, a session-cap feature, a
  Windows PTY bridge). Breadth in the product is a goal, not a footprint
  concern — as long as it integrates with the existing setup/config UX
  (`hermes tools`, `hermes setup`, auto-install) rather than bolting on a raw
  env var.
- **Refactor god-files into clean modules.** Extracting a multi-thousand-line
  cluster out of `cli.py` / `run_agent.py` / `gateway/run.py` into a focused
  mixin or module is wanted work, even when the diff is huge and mechanical
  (large `+N/-N` refactors merge regularly). The "every line traces to the
  request" test applies to *feature* PRs; a declared refactor's request IS the
  extraction.
- **Keep the core narrow.** New *model tools* are the expensive exception —
  every tool ships on every API call. Prefer, in order: extend existing code →
  CLI command + skill → service-gated tool (`check_fn`) → plugin → MCP server
  in the catalog → new core tool (last resort). See "The Footprint Ladder."
- **Extend, don't duplicate.** Before adding a module/manager/hook, check
  whether existing infrastructure already covers the use case. When several PRs
  integrate the same *category*, design one shared interface instead of merging
  them one at a time (see the ABC + orchestrator note under the Footprint
  Ladder).
- **Behavior contracts over snapshots.** Tests should assert how two pieces of
  data must relate (invariants), not freeze a current value (model lists,
  config version literals, enumeration counts). See "Don't write
  change-detector tests."
- **E2E validation, not just green unit mocks.** For anything touching
  resolution chains, config propagation, security boundaries, remote
  backends, or file/network I/O, exercise the real path with real imports
  against a temp `HERMES_HOME`. Mocks hide integration bugs.
- **Cache-, alternation-, and invariant-safe.** Preserve prompt caching, strict
  message role alternation (never two same-role messages in a row; never a
  synthetic user message injected mid-loop), and a system prompt that is
  byte-stable for the life of a conversation.
- **Contributor credit preserved.** Salvage external work by cherry-picking
  (rebase-merge) so authorship survives in git history; don't reimplement from
  scratch when you can build on top.

### What we don't want (rejected even when well-built)

- **Speculative infrastructure.** Hooks, callbacks, or extension points with no
  concrete consumer. Adding a hook is easy; removing one after plugins depend
  on it is hard. A hook is NOT speculative if a contributor has a real, stated
  use case — even if the consumer ships separately.
- **New `HERMES_*` env vars for non-secret config.** `.env` is for secrets
  only (API keys, tokens, passwords). All behavioral settings — timeouts,
  thresholds, feature flags, display prefs — go in `config.yaml`. Bridge to an
  internal env var if the mechanism needs one, but user-facing docs point to
  `config.yaml`. Reject PRs that tell users to "set X in your .env" unless X
  is a credential.
- **A new core tool when terminal + file already do the job, or when a skill
  would.** If the only barrier is file visibility on a remote backend, fix the
  mount, not the toolset.
- **Lazy-reading escape hatches on instructional tools.** No `offset`/`limit`
  pagination on tools that load content the agent must read fully (skills,
  prompts, playbooks). Models will read page 1 and skip the rest.
- **"Fixes" that destroy the feature they secure.** A mitigation that kills the
  feature's purpose is the wrong mitigation. Read the original commit's intent
  (`git log -p -S`) before restricting behavior; find a fix that preserves the
  feature.
- **Outbound telemetry / usage attribution without opt-in gating.** No new
  analytics, third-party identifier tagging, or attribution tags until a
  generic user-facing opt-in (config gate + setup prompt + `hermes tools`
  toggle) exists. Park behind a label, do not merge.
- **Change-detector tests, cache-breaking mid-conversation, dead code wired in
  without E2E proof, and plugins that touch core files.** Plugins live in their
  own directory and work within the ABCs/hooks we provide; if a plugin needs
  more, widen the generic plugin surface, don't special-case it in core.
- **Third-party products / other people's projects integrated into the core
  tree.** Observability backends, vendor SaaS integrations, analytics dashboards,
  and similar "someone else's product" plugins do NOT land under `plugins/` in
  this repo. They place an ongoing maintenance burden on us to keep them working
  against a fast-moving core, for a backend we don't own. Ship them as a
  **standalone plugin repo** users install into `~/.hermes/plugins/` (or via a
  pip entry point), and promote them in the Nous Research Discord
  (`#plugins-skills-and-skins`). This is a coupling-and-maintenance decision, not
  a quality bar — the plugin can be excellent and still be a close. PRs that add
  such a directory to the tree are closed with a pointer to publish it as its own
  repo.

### Before you call it a bug — verify the premise (and when NOT to close)

The most common reason a well-written PR gets closed is not code quality — it
is that the change is built on a **wrong premise**, or it treats an
**intentional design as a gap**. These patterns cut both ways: they tell a
human reviewer what to scrutinize, and they tell the automated sweeper when a
PR is NOT safe to close as `implemented_on_main` / `cannot_reproduce` (when in
doubt, leave it open for a human). They are distilled from real closes.

- **"Intentional design, not a gap."** A limitation that looks like an
  oversight is often deliberate. Before "fixing" a missing link or a
  restriction, ask whether the isolation IS the design. Example: profiles are
  independent islands on purpose — a PR adding live config inheritance from the
  default profile was closed because coupling profiles together is exactly what
  the design prevents (the copy-at-creation `--clone` path already covers the
  legitimate "start from my default" case). Read the original commit's intent
  (`git log -p -S "<symbol>"`) before assuming something is unfinished.
- **"The premise doesn't hold against how X actually works."** A PR's
  justification frequently rests on a wrong mental model of an existing
  mechanism. Trace the real code/runtime before accepting the rationale. Two
  real closes: a rate-limit "re-probe during cooldown" PR (the breaker only
  trips on a *confirmed-empty* account bucket, so re-probing just hammers a
  bucket we've already proven empty); a usage-accumulation fix whose new branch
  **never executes at runtime** because an earlier guard already popped the
  state it depended on. If you can't point to the exact line where the bug
  manifests AND show the fix changes that line's behavior, you haven't verified
  the premise.
- **"This fix was wrong — the absence/omission was deliberate."** Adding the
  obvious-looking missing piece can break things the omission was protecting.
  Example: restoring "missing" `__init__.py` files made a test tree importable
  as a dotted package that shadowed the real plugin, deleting its `register()`
  at import time. The absence was load-bearing.
- **"Overreached / resurrected an approach we'd moved past."** Scope creep that
  supersedes an agreed-on base, or revives a direction the maintainers
  deliberately closed, gets rejected even when the code works. Keep the change
  to the narrow piece that was actually agreed; offer the rest as a focused
  follow-up.

The throughline: **verify the claim AND the intent against the codebase before
writing or merging a fix.** A confirmed reproduction on current `main` plus a
line-level account of where the fix acts beats a plausible-sounding rationale
every time. When in doubt about intent, it is cheaper to ask than to ship a
fix that fights the design.

### The Footprint Ladder (new capability decision)

Each rung adds more permanent surface than the one above. Choose the highest
(least-footprint) rung that correctly solves the problem:

1. **Extend existing code** — the capability is a variation of something that
   already exists. Zero new surface.
2. **CLI command + skill** — manages config/state/infra expressible as shell
   commands. The agent runs `hermes <subcommand>` guided by a skill. Zero
   model-tool footprint. Default choice for subscriptions, scheduled tasks,
   service setup. Examples: `hermes webhook`, `hermes cron`, `hermes tools`.
3. **Service-gated tool (`check_fn`)** — needs structured params/returns AND
   only appears when a prerequisite is configured. Zero footprint otherwise.
   Examples: Home Assistant tools (gated on token), memory-provider tools.
4. **Plugin** — third-party/niche/user-specific capability that doesn't ship in
   core. Lives in `~/.hermes/plugins/` or a pip package, discovered at runtime.
5. **MCP server (in the catalog)** — if the capability genuinely needs to be a
   tool (structured I/O the agent invokes) but isn't core-fundamental, prefer
   building it as an MCP server and adding it to the MCP catalog over growing
   the core toolset. The agent connects to it through the built-in MCP client;
   zero permanent core-schema footprint, and it's reusable by any MCP host.
6. **New core tool** — only when the capability is fundamental, broadly useful
   to nearly every user, and unreachable via terminal + file (or an MCP server).
   Examples of correct core tools: terminal, read_file, web_search,
   browser_navigate.

When 3+ open PRs try to integrate the same *category* of thing (memory
backends, providers, notifiers), don't merge them one at a time — design an
ABC + orchestrator, wrap the existing built-in as the first provider, and turn
the competing PRs into plugins against that interface.

### Surface capability is a property of the SESSION, never of the process env

A tool that only works because of *who is on the other end of the connection* —
the desktop app's panes, the in-app browser, message reactions, Projects — must
resolve its availability from the **session's own source**, not from an env var
on the backend process.

The client and the backend are separate machines on separate clocks. The
desktop app can be driving a backend Electron spawned locally, one over SSH,
one behind a plain URL + token, or Hermes Cloud. Only the first two are spawned
by us and carry `HERMES_DESKTOP=1`. Every env-keyed GUI gate is therefore a
silent no-op on the other half of the topologies, and the failure is invisible:
the tool is stripped from the schema before the model ever sees it, on the same
backend whose platform hint is telling the model it's *"chatting inside the
Hermes desktop app."*

The pattern that works:

- **The toolset is the surface gate.** Keep the tools off `_HERMES_CORE_TOOLS`
  (nobody else should pay their schema) and put them in a named toolset —
  `desktop_ui`, `project`. The GUI gateway's `_load_enabled_toolsets(platform)`
  folds that toolset in when the session's platform says GUI. One resolver,
  every topology.
- **`check_fn` answers reachability or user opt-in, not surface.** "Is the
  renderer bridge wired?", "did the user enable reactions?" — fine. "Was I
  spawned by Electron?" — not fine. `check_fn` results are also TTL-cached
  process-wide (`tools/registry.py`), so a per-session answer does not belong
  there at all: one process serves many sessions.
- **Ask which identity you actually mean.** `HERMES_DESKTOP=1` legitimately
  marks *"this backend process was spawned by the app"* — it gates the cron
  ticker and web-dist handling correctly. It does NOT mean "a GUI is watching",
  and the embedded terminal pane (`hermes --tui` against that same backend) is
  the standing counterexample.

Same test both ways: if the capability would still make sense with the client
on another machine, it is session-scoped. Cover it with a test that asserts the
GUI session gets the tool **with the env var absent** — that's the assertion
the original gate could never have passed.

## Development Environment

```bash
# Prefer .venv; fall back to venv if that's what your checkout has.
source .venv/bin/activate   # or: source venv/bin/activate
```

`scripts/run_tests.sh` probes `.venv` first, then `venv`, then
`$HOME/.hermes/hermes-agent/venv` (for worktrees that share a venv with the
main checkout).

## Project Structure

File counts shift constantly — don't treat the tree below as exhaustive.
The canonical source is the filesystem. The notes call out the load-bearing
entry points you'll actually edit.

```
hermes-agent/
├── run_agent.py          # AIAgent class — core conversation loop (~12k LOC)
├── model_tools.py        # Tool orchestration, discover_builtin_tools(), handle_function_call()
├── toolsets.py           # Toolset definitions, _HERMES_CORE_TOOLS list
├── cli.py                # HermesCLI class — interactive CLI orchestrator (~11k LOC)
├── hermes_state.py       # SessionDB — SQLite session store (FTS5 search)
├── hermes_constants.py   # get_hermes_home(), display_hermes_home() — profile-aware paths
├── hermes_logging.py     # setup_logging() — agent.log / errors.log / gateway.log (profile-aware)
├── batch_runner.py       # Parallel batch processing
├── agent/                # Agent internals (provider adapters, memory, caching, compression, etc.)
├── hermes_cli/           # CLI subcommands, setup wizard, plugins loader, skin engine
├── tools/                # Tool implementations — auto-discovered via tools/registry.py
│   └── environments/     # Terminal backends (local, docker, ssh, modal, daytona, singularity)
├── gateway/              # Messaging gateway — run.py + session.py + platforms/
│   ├── platforms/        # Adapter per platform (telegram, discord, slack, whatsapp,
│   │                     #   homeassistant, signal, matrix, mattermost, email, sms,
│   │                     #   dingtalk, wecom, weixin, feishu, qqbot, bluebubbles,
│   │                     #   yuanbao, webhook, api_server, ...). See ADDING_A_PLATFORM.md.
│   └── builtin_hooks/    # Extension point for always-registered gateway hooks (none shipped)
├── plugins/              # Plugin system (see "Plugins" section below)
│   ├── memory/           # Memory-provider plugins (honcho, mem0, supermemory, ...)
│   ├── context_engine/   # Context-engine plugins
│   ├── model-providers/  # Inference backend plugins (openrouter, anthropic, gmi, ...)
│   ├── kanban/           # Multi-agent board dispatcher + worker plugin
│   ├── hermes-achievements/  # Gamified achievement tracking
│   ├── observability/    # Metrics / traces / logs plugin
│   ├── image_gen/        # Image-generation providers
│   └── <others>/         # disk-cleanup, google_meet, platforms, spotify,
│                         #   strike-freedom-cockpit, ...
├── optional-skills/      # Heavier/niche skills shipped but NOT active by default
├── skills/               # Built-in skills bundled with the repo
├── ui-tui/               # Ink (React) terminal UI — `hermes --tui`
│   └── src/              # entry.tsx, app.tsx, gatewayClient.ts + app/components/hooks/lib
├── tui_gateway/          # Python JSON-RPC backend for the TUI
├── acp_adapter/          # ACP server (VS Code / Zed / JetBrains integration)
├── cron/                 # Scheduler — jobs.py, scheduler.py
├── scripts/              # run_tests.sh, release.py, auxiliary scripts
├── website/              # Docusaurus docs site
└── tests/                # Pytest suite (~17k tests across ~900 files as of May 2026)
```

**User config:** `~/.hermes/config.yaml` (settings), `~/.hermes/.env` (API keys only).
**Logs:** `~/.hermes/logs/` — `agent.log` (INFO+), `errors.log` (WARNING+),
`gateway.log` when running the gateway. Profile-aware via `get_hermes_home()`.
Browse with `hermes logs [--follow] [--level ...] [--session ...]`.

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

## AIAgent Class (run_agent.py)

The real `AIAgent.__init__` takes ~60 parameters (credentials, routing, callbacks,
session context, budget, credential pool, etc.). The signature below is the
minimum subset you'll usually touch — read `run_agent.py` for the full list.

```python
class AIAgent:
    def __init__(self,
        base_url: str = None,
        api_key: str = None,
        provider: str = None,
        api_mode: str = None,              # "chat_completions" | "codex_responses" | ...
        model: str = "",                   # empty → resolved from config/provider later
        max_iterations: int = 500,         # tool-calling iterations (shared with subagents)
        enabled_toolsets: list = None,
        disabled_toolsets: list = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str = None,              # "cli", "telegram", etc.
        session_id: str = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        credential_pool=None,
        # ... plus callbacks, thread/user/chat IDs, iteration_budget, fallback_model,
        # checkpoints config, prefill_messages, service_tier, reasoning_config, etc.
    ): ...

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""

    def run_conversation(self, user_message: str, system_message: str = None,
                         conversation_history: list = None, task_id: str = None) -> dict:
        """Full interface — returns dict with final_response + messages."""
```

### Agent Loop

The core loop is inside `run_conversation()` — entirely synchronous, with
interrupt checks, budget tracking, and a one-turn grace call:

```python
while (api_call_count < self.max_iterations and self.iteration_budget.remaining > 0) \
        or self._budget_grace_call:
    if self._interrupt_requested: break
    response = client.chat.completions.create(model=model, messages=messages, tools=tool_schemas)
    if response.tool_calls:
        for tool_call in response.tool_calls:
            result = handle_function_call(tool_call.name, tool_call.args, task_id)
            messages.append(tool_result_message(result))
        api_call_count += 1
    else:
        return response.content
```

Messages follow OpenAI format: `{"role": "system/user/assistant/tool", ...}`.
Reasoning content is stored in `assistant_msg["reasoning"]`.

---

## CLI Architecture (cli.py)

- **Rich** for banner/panels, **prompt_toolkit** for input with autocomplete
- **KawaiiSpinner** (`agent/display.py`) — animated faces during API calls, `┊` activity feed for tool results
- `load_cli_config()` in cli.py merges hardcoded defaults + user config YAML
- **Skin engine** (`hermes_cli/skin_engine.py`) — data-driven CLI theming; initialized from `display.skin` config key at startup; skins customize banner colors, spinner faces/verbs/wings, tool prefix, response box, branding text
- `process_command()` is a method on `HermesCLI` — dispatches on canonical command name resolved via `resolve_command()` from the central registry
- Skill slash commands: `agent/skill_commands.py` scans `~/.hermes/skills/`, injects as **user message** (not system prompt) to preserve prompt caching

### Slash Command Registry (`hermes_cli/commands.py`)

All slash commands are defined in a central `COMMAND_REGISTRY` list of `CommandDef` objects. Every downstream consumer derives from this registry automatically:

- **CLI** — `process_command()` resolves aliases via `resolve_command()`, dispatches on canonical name
- **Gateway** — `GATEWAY_KNOWN_COMMANDS` frozenset for hook emission, `resolve_command()` for dispatch
- **Gateway help** — `gateway_help_lines()` generates `/help` output
- **Telegram** — `telegram_bot_commands()` generates the BotCommand menu
- **Slack** — `slack_subcommand_map()` generates `/hermes` subcommand routing
- **Autocomplete** — `COMMANDS` flat dict feeds `SlashCommandCompleter`
- **CLI help** — `COMMANDS_BY_CATEGORY` dict feeds `show_help()`

### Adding a Slash Command

1. Add a `CommandDef` entry to `COMMAND_REGISTRY` in `hermes_cli/commands.py`:
```python
CommandDef("mycommand", "Description of what it does", "Session",
           aliases=("mc",), args_hint="[arg]"),
```
2. Add handler in `HermesCLI.process_command()` in `cli.py`:
```python
elif canonical == "mycommand":
    self._handle_mycommand(cmd_original)
```
3. If the command is available in the gateway, add a handler in `gateway/run.py`:
```python
if canonical == "mycommand":
    return await self._handle_mycommand(event)
```
4. For persistent settings, use `save_config_value()` in `cli.py`

**CommandDef fields:**
- `name` — canonical name without slash (e.g. `"background"`)
- `description` — human-readable description
- `category` — one of `"Session"`, `"Configuration"`, `"Tools & Skills"`, `"Info"`, `"Exit"`
- `aliases` — tuple of alternative names (e.g. `("bg",)`)
- `args_hint` — argument placeholder shown in help (e.g. `"<prompt>"`, `"[name]"`)
- `cli_only` — only available in the interactive CLI
- `gateway_only` — only available in messaging platforms
- `gateway_config_gate` — config dotpath (e.g. `"display.tool_progress_command"`); when set on a `cli_only` command, the command becomes available in the gateway if the config value is truthy. `GATEWAY_KNOWN_COMMANDS` always includes config-gated commands so the gateway can dispatch them; help/menus only show them when the gate is open.

**Adding an alias** requires only adding it to the `aliases` tuple on the existing `CommandDef`. No other file changes needed — dispatch, help text, Telegram menu, Slack mapping, and autocomplete all update automatically.

---

## TUI Architecture (ui-tui + tui_gateway)

The TUI is a full replacement for the classic (prompt_toolkit) CLI, activated via `hermes --tui` or `HERMES_TUI=1`.

### Process Model

```
hermes --tui
  └─ Node (Ink)  ──stdio JSON-RPC──  Python (tui_gateway)
       │                                  └─ AIAgent + tools + sessions
       └─ renders transcript, composer, prompts, activity
```

TypeScript owns the screen. Python owns sessions, tools, model calls, and slash command logic.

### Transport

Newline-delimited JSON-RPC over stdio. Requests from Ink, events from Python. See `tui_gateway/server.py` for the full method/event catalog.

### Key Surfaces

| Surface | Ink component | Gateway method |
|---------|---------------|----------------|
| Chat streaming | `app.tsx` + `messageLine.tsx` | `prompt.submit` → `message.delta/complete` |
| Tool activity | `thinking.tsx` | `tool.start/progress/complete` |
| Approvals | `prompts.tsx` | `approval.respond` ← `approval.request` |
| Clarify/sudo/secret | `prompts.tsx`, `maskedPrompt.tsx` | `clarify/sudo/secret.respond` |
| Session picker | `sessionPicker.tsx` | `session.list/resume` |
| Slash commands | Local handler + fallthrough | `slash.exec` → `_SlashWorker`, `command.dispatch` |
| Completions | `useCompletion` hook | `complete.slash`, `complete.path` |
| Theming | `theme.ts` + `branding.tsx` | `gateway.ready` with skin data |

### Slash Command Flow

1. Built-in client commands (`/help`, `/quit`, `/clear`, `/resume`, `/copy`, `/paste`, etc.) handled locally in `app.tsx`
2. Everything else → `slash.exec` (runs in persistent `_SlashWorker` subprocess) → `command.dispatch` fallback

### Dev Commands

```bash
cd ui-tui
npm install       # first time
npm run dev       # watch mode (rebuilds hermes-ink + tsx --watch)
npm start         # production
npm run build     # full build (hermes-ink + tsc)
npm run typecheck # typecheck only (tsc --noEmit)
npm run lint      # eslint
npm run fmt       # prettier
npm test          # vitest
```

### TUI in the Dashboard (`hermes dashboard` → `/chat`)

The dashboard embeds the real `hermes --tui` — **not** a rewrite.  See `hermes_cli/pty_bridge.py` + the `@app.websocket("/api/pty")` endpoint in `hermes_cli/web_server.py`.

- Browser loads `web/src/pages/ChatPage.tsx`, which mounts xterm.js's `Terminal` with the WebGL renderer, `@xterm/addon-fit` for container-driven resize, and `@xterm/addon-unicode11` for modern wide-character widths.
- `/api/pty?token=…` upgrades to a WebSocket; auth uses the same ephemeral `_SESSION_TOKEN` as REST, via query param (browsers can't set `Authorization` on WS upgrade).
- The server spawns whatever `hermes --tui` would spawn, through `ptyprocess` (POSIX PTY — WSL works, native Windows does not).
- Frames: raw PTY bytes each direction; resize via `\x1b[RESIZE:<cols>;<rows>]` intercepted on the server and applied with `TIOCSWINSZ`.

**Do not re-implement the primary chat experience in React.** The main transcript, composer/input flow (including slash-command behavior), and PTY-backed terminal belong to the embedded `hermes --tui` — anything new you add to Ink shows up in the dashboard automatically. If you find yourself rebuilding the transcript or composer for the dashboard, stop and extend Ink instead.

**Structured React UI around the TUI is allowed when it is not a second chat surface.** Sidebar widgets, inspectors, summaries, status panels, and similar supporting views (e.g. `ChatSidebar`, `ModelPickerDialog`, `ToolCall`) are fine when they complement the embedded TUI rather than replacing the transcript / composer / terminal. Keep their state independent of the PTY child's session and surface their failures non-destructively so the terminal pane keeps working unimpaired.

### Electron Desktop Chat App (`apps/desktop/`)

A **separate** chat surface from both the classic CLI and the dashboard's embedded TUI. It is an Electron + React + nanostore renderer (`@assistant-ui/react`) that talks to a `tui_gateway` backend over JSON-RPC (`requestGateway(method, params)`). The WebSocket/JSON-RPC transport lives in the framework-agnostic `apps/shared` package (`@hermes/shared` — `JsonRpcGatewayClient` + WS URL helpers), which the web dashboard (`web/`) also consumes; **desktop has no build/runtime dependency on the dashboard frontend** — it spawns a headless `hermes serve` backend server (the same gateway `dashboard` serves, minus the browser UI entirely: `serve` sets `headless_backend=True`, so `cmd_dashboard` skips `_build_web_ui` AND exports `HERMES_SERVE_HEADLESS=1` so `mount_spa()` disables the SPA even if a stray `web_dist/` exists — only the JSON-RPC/WS/API surface is reachable). `dashboard` and `serve` share `cmd_dashboard`/`start_server` but are independent surfaces — neither launches the other. The one exception is a backward-compat *fallback*: `serve` is newer, so the desktop spawn (`electron/backend-command.ts` + `backendSupportsServe()` in `electron/main.ts`) detects whether the resolved runtime registers `serve` and, only when it does not (an older managed install / PATH `hermes` the app hasn't updated yet), rewrites the argv to the legacy `dashboard --no-open`. Without that, a new app against an un-upgraded runtime would crash on an unknown subcommand and brick every mid-upgrade user. It does NOT embed `hermes --tui` — it has its own composer, transcript, and slash-command pipeline. For scoped Desktop architecture, state, resolver, transport, and testing rules, read `apps/desktop/AGENTS.md`.

**Slash commands in the desktop app are curated client-side, then dispatched to the backend.** The pipeline:

- **Backend already provides everything.** `tui_gateway/server.py` `commands.catalog` (empty-query list) and `complete.slash` (typed-query completions) both include built-in commands, user `quick_commands`, AND skill-derived commands (`scan_skill_commands()` / `get_skill_commands()`). The desktop app does not need a new RPC to see skills.
- **The renderer curates via `apps/desktop/src/lib/desktop-slash-commands.ts`.** This is the load-bearing file. It holds `DESKTOP_COMMAND_SPECS` (the built-ins and their Desktop surfaces) plus `NO_DESKTOP_SURFACE` block-lists for terminal-only / messaging-only / picker-owned / settings-owned / advanced commands that should NOT clutter the desktop popover.
  - `isDesktopSlashCommand(name)` — gates **execution**. Returns true for built-ins AND for any non-built-in (skill / quick command), so typed extension commands run.
  - `isDesktopSlashSuggestion(name)` — gates **discovery/completion**. Used by BOTH completion paths in `app/chat/composer/hooks/use-slash-completions.ts` (empty-query catalog filter + typed-query `complete.slash` filter) and by `filterDesktopCommandsCatalog`.
  - `isDesktopSlashExtensionCommand(name)` — true when the command is NOT a known Hermes built-in (i.e. a skill or user quick command). Both suggestion and catalog-filter paths allow extensions through so skill commands surface in the palette. (Added when fixing "skill commands missing from the desktop slash palette" — the curated allow-list was silently dropping every skill/quick command from completions even though they executed fine when typed.)
- **Dispatch** lives in `app/session/hooks/use-prompt-actions/slash.ts` (`runSlash`): built-ins that the desktop owns (`/skin`, `/help`, `/new`, …) are handled locally or via `commands.catalog`; everything else goes to `slash.exec`, falling back to `command.dispatch` (which the gateway resolves into skill / alias / exec directives). A skill command resolves to `{type: "skill", message}` and is submitted as a normal prompt.

**Rule:** the desktop slash palette's curation is about hiding noise (terminal-only / messaging-only built-ins), NOT about hiding user-activated extensions. Skill commands and `quick_commands` are extensions the backend surfaces — they belong in completions. If you tighten `desktop-slash-commands.ts`, keep `isDesktopSlashExtensionCommand` flowing into both the suggestion and catalog-filter paths. Tests: from `apps/desktop`, run `npx vitest run src/lib/desktop-slash-commands.test.ts` (workspace dependencies are installed at the repo root).

---

## Adding New Tools

Before adding any tool, settle the footprint question first (see "The
Footprint Ladder" in the Contribution Rubric): most capabilities should NOT
be core tools. For custom or local-only tools, do **not** edit Hermes core.
Use the plugin route instead: create `~/.hermes/plugins/<name>/plugin.yaml`
and `~/.hermes/plugins/<name>/__init__.py`, then register tools with
`ctx.register_tool(...)`. Plugin toolsets are discovered automatically and can be
enabled or disabled without touching `tools/` or `toolsets.py`.

Use the built-in route below only when the user is explicitly contributing a new
core Hermes tool that should ship in the base system.

Built-in/core tools require changes in **2 files**:

**1. Create `tools/your_tool.py`:**
```python
import json, os
from tools.registry import registry

def check_requirements() -> bool:
    return bool(os.getenv("EXAMPLE_API_KEY"))

def example_tool(param: str, task_id: str = None) -> str:
    return json.dumps({"success": True, "data": "..."})

registry.register(
    name="example_tool",
    toolset="example",
    schema={"name": "example_tool", "description": "...", "parameters": {...}},
    handler=lambda args, **kw: example_tool(param=args.get("param", ""), task_id=kw.get("task_id")),
    check_fn=check_requirements,
    requires_env=["EXAMPLE_API_KEY"],
)
```

**2. Add to `toolsets.py`** — either `_HERMES_CORE_TOOLS` (all platforms) or a new toolset. **This step is required:** auto-discovery imports the tool and registers its schema, but the tool is only *exposed to an agent* if its name appears in a toolset. `_HERMES_CORE_TOOLS` is not dead code — it's the default bundle every platform's base toolset inherits from.

Auto-discovery: any `tools/*.py` file with a top-level `registry.register()` call is imported automatically — no manual import list to maintain. Wiring into a toolset is still a deliberate, manual step.

The registry handles schema collection, dispatch, availability checking, and error wrapping. All handlers MUST return a JSON string.

**Path references in tool schemas**: If the schema description mentions file paths (e.g. default output directories), use `display_hermes_home()` to make them profile-aware. The schema is generated at import time, which is after `_apply_profile_override()` sets `HERMES_HOME`.

**State files**: If a tool stores persistent state (caches, logs, checkpoints), use `get_hermes_home()` for the base directory — never `Path.home() / ".hermes"`. This ensures each profile gets its own state.

**Agent-level tools** (todo, memory): intercepted by `run_agent.py` before `handle_function_call()`. See `tools/todo_tool.py` for the pattern.

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

## Adding Configuration

### config.yaml options:
1. Add to `DEFAULT_CONFIG` in `hermes_cli/config.py`
2. Bump `_config_version` (check the current value at the top of `DEFAULT_CONFIG`)
   ONLY if you need to actively migrate/transform existing user config
   (renaming keys, changing structure). Adding a new key to an existing
   section is handled automatically by the deep-merge and does NOT require
   a version bump.

### Top-level `config.yaml` sections (non-exhaustive):

`model`, `agent`, `terminal`, `compression`, `display`, `stt`, `tts`,
`memory`, `security`, `delegation`, `smart_model_routing`, `checkpoints`,
`auxiliary`, `curator`, `skills`, `gateway`, `logging`, `cron`, `profiles`,
`plugins`, `honcho`.

`auxiliary` holds per-task overrides for side-LLM work (curator, vision,
embedding, title generation, session_search, etc.) — each task can pin
its own provider/model/base_url/max_tokens/reasoning_effort. See
`agent/auxiliary_client.py::_resolve_auto` for resolution order.

`curator` holds the background skill-maintenance config —
`enabled`, `interval_hours`, `min_idle_hours`, `stale_after_days`,
`archive_after_days`, `backup` (nested).

### .env variables (SECRETS ONLY — API keys, tokens, passwords):
1. Add to `OPTIONAL_ENV_VARS` in `hermes_cli/config.py` with metadata:
```python
"NEW_API_KEY": {
    "description": "What it's for",
    "prompt": "Display name",
    "url": "https://...",
    "password": True,
    "category": "tool",  # provider, tool, messaging, setting
},
```

Non-secret settings (timeouts, thresholds, feature flags, paths, display
preferences) belong in `config.yaml`, not `.env`. If internal code needs an
env var mirror for backward compatibility, bridge it from `config.yaml` to
the env var in code (see `gateway_timeout`, `terminal.cwd` → `TERMINAL_CWD`).

### Config loaders (three paths — know which one you're in):

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` — merges CLI-specific defaults + user YAML |
| `load_config()` | `hermes tools`, `hermes setup`, most CLI subcommands | `hermes_cli/config.py` — merges `DEFAULT_CONFIG` + user YAML |
| Direct YAML load | Gateway runtime | `gateway/run.py` + `gateway/config.py` — reads user YAML raw |

If you add a new key and the CLI sees it but the gateway doesn't (or vice
versa), you're on the wrong loader. Check `DEFAULT_CONFIG` coverage.

### Working directory:
- **CLI** — uses the process's current directory (`os.getcwd()`).
- **Messaging** — uses `terminal.cwd` from `config.yaml`. The gateway bridges this
  to the `TERMINAL_CWD` env var for child tools. **`MESSAGING_CWD` has been
  removed** — the config loader prints a deprecation warning if it's set in
  `.env`. Same for `TERMINAL_CWD` in `.env`; the canonical setting is
  `terminal.cwd` in `config.yaml`.

---

## Skin/Theme System

The skin engine (`hermes_cli/skin_engine.py`) provides data-driven CLI visual customization. Skins are **pure data** — no code changes needed to add a new skin.

### Architecture

## What Hermes Is

Hermes is a personal AI agent with one core across CLI, messaging gateway
(~20 platforms), TUI, and Electron desktop. Capability grows via **plugins and
skills**, not by thickening the core.

Two sacred properties:

- **Per-conversation prompt caching is sacred.** Do not mutate past context,
  swap toolsets, or rebuild the system prompt mid-conversation (exception:
  context compression).
- **Narrow waist; capability at the edges.** Prefer: extend existing → CLI +
  skill → service-gated tool (`check_fn`) → plugin → MCP catalog → new core
  tool (last resort).

## Fork Overlay

This checkout keeps fork-only behaviour at extension and ops edges. Before an
upstream merge or fork-owned edit, read [`fork/AGENTS.md`](fork/AGENTS.md) and
the nested guide. Authoritative merge mechanism:
`scripts/merge_tools/` / `scripts/sync_all.py` — upstream base, reapply verified
fork advantages only. Detail catalog:
[Fork-specific features](fork/harness/upstream-development-guide.md#fork-specific-features-for-ai-agents).

| Need | Guide |
|------|--------|
| Merge / overlays | [`fork/harness/AGENTS.md`](fork/harness/AGENTS.md) |
| Hypura `harness_*` / daemon | [`fork/agent-harness/AGENTS.md`](fork/agent-harness/AGENTS.md) |
| Plugins / fork tools | [`fork/extensions/AGENTS.md`](fork/extensions/AGENTS.md) |
| Windows stack / ports | [`fork/operations/AGENTS.md`](fork/operations/AGENTS.md) |
| Root scratch | [`fork/local-workspace/AGENTS.md`](fork/local-workspace/AGENTS.md) |

Secrets in `.env` only; non-secret behaviour in `config.yaml`. No new non-secret
`HERMES_*` env vars. Do not commit `_docs/`, media, release bundles, or
`node_modules/`.

## Root Layout Policy

Match upstream root style: packaging/entry Python (`run_agent.py`, `cli.py`,
`model_tools.py`, …), `scripts/`, `docs/`, `tests/`, apps/UI packages stay at
their official paths — **do not relocate** for “cleanliness” (imports, CI, and
`pyproject` assume them).

| Stay at root (intentional) | Why |
|----------------------------|-----|
| Core `*.py` entry modules, `pyproject.toml`, lockfiles | Official packaging surface |
| `hermes_api_server.py`, `sync_memory.py`, `requirements.txt` | Fork helpers wired by tests/scripts |
| `fork/`, `vendor/`, `brain/`, `SOUL.md` (local) | Fork / identity; not upstream PR material |

| Move / classify | Destination |
|-----------------|-------------|
| One-off probes, `tmp_*.py`, tweet drafts, comparison notes | `tmp/probes/` or `output/reports/` (gitignored) |
| Tracked operator notes (`TASK_SUMMARY`, release tweet drafts) | `fork/local-workspace/notes/` |
| Generated media / logs / `_docs` | `output/*`, `_docs/` — never publish |

See [`fork/local-workspace/README.md`](fork/local-workspace/README.md).

### Python
**ALWAYS use `scripts/run_tests.sh`** — do not call `pytest` directly. The script enforces
hermetic environment parity with CI (unset credential vars, TZ=UTC, LANG=C.UTF-8,
per-file subprocess isolation via `scripts/run_tests_parallel.py` — no xdist,
worker count auto-scaled from CPU count). Direct `pytest`
on a 16+ core developer machine with API keys set diverges from CI in ways
that have caused multiple "works locally, fails in CI" incidents (and the reverse).

```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py -k test_x  # one test (file + -k; the runner is file-granular)
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
```

**Flake policy:** the runner auto-retries a failing test FILE once in a fresh
subprocess (`--file-retries`, default 1; `HERMES_TEST_FILE_RETRIES=0` to
disable). Pass-on-retry counts as green but is printed in a `⚠ FLAKY` summary
section with both attempts' output. A FLAKY report is a bug to fix, not noise
to ignore — timing-sensitive tests must not assume a quiet runner (loose
wall-clock bounds ≥ 2s, event-based sync, no `assert not _wait_until(...)`
negative-timing races).

#### Subprocess-per-test-file isolation

Every test file runs in a freshly-spawned Python subprocess via `run_tests_parallel.py`. This means module-level dicts/sets and
ContextVars from one test file cannot leak into the next.

#### Why the wrapper

|                     | Without wrapper                             | With wrapper                              |
| ------------------- | ------------------------------------------- | ----------------------------------------- |
| Provider API keys   | Whatever is in your env (auto-detects pool) | All env vars except a specific few unset. |
| HOME / `~/.hermes/` | Your real config+auth.json                  | Temp dir per test                         |
| Timezone            | Local TZ (PDT etc.)                         | UTC                                       |
| Locale              | Whatever is set                             | C.UTF-8                                   |

### Where to place what tests

The CI change classifier (`scripts/ci/classify_changes.py`) runs specific jobs based on what files changed. A Python test that asserts
about the contents of `package.json`, `package-lock.json`, `.ts`/`.tsx`
source, or any other JS-side artifact will not run on a PR that only touches
those files. This means a regression can go green on a PR and red on `main` (where the
classifier fails open and runs everything).

Any test that reads or asserts about `package.json`,
`package-lock.json`, `tsconfig.json`, `.ts`/`.tsx`/`.js`/`.mjs`/`.cjs`
source files configuration belongs in the JS (vitest) test suite, not in `tests/*.py`.

### Don't write change-detector tests

A test is a **change-detector** if it fails whenever data that is **expected
to change** gets updated — model catalogs, config version numbers,
enumeration counts, hardcoded lists of provider models. These tests add no
behavioral coverage; they just guarantee that routine source updates break
CI and cost engineering time to "fix."

**Do not write:**

```python
# catalog snapshot — breaks every model release
assert "gemini-2.5-pro" in _PROVIDER_MODELS["gemini"]
assert "MiniMax-M2.7" in models

# config version literal — breaks every schema bump
assert DEFAULT_CONFIG["_config_version"] == 21

# enumeration count — breaks every time a skill/provider is added
assert len(_PROVIDER_MODELS["huggingface"]) == 8
```

**Do write:**

```python
# behavior: does the catalog plumbing work at all?
assert "gemini" in _PROVIDER_MODELS
assert len(_PROVIDER_MODELS["gemini"]) >= 1

# behavior: does migration bump the user's version to current latest?
assert raw["_config_version"] == DEFAULT_CONFIG["_config_version"]

# invariant: no plan-only model leaks into the legacy list
assert not (set(moonshot_models) & coding_plan_only_models)

# invariant: every model in the catalog has a context-length entry
for m in _PROVIDER_MODELS["huggingface"]:
    assert m.lower() in DEFAULT_CONTEXT_LENGTHS_LOWER
```

The rule: if the test reads like a snapshot of current data, delete it. If
it reads like a contract about how two pieces of data must relate, keep it.
When a PR adds a new provider/model and you want a test, make the test
assert the relationship (e.g. "catalog entries all have context lengths"),
not the specific names.

Reviewers should reject new change-detector tests; authors should convert
them into invariants before re-requesting review.

### Never read source code in tests

A test that reads a source file's text is testing *the shape of the
source code*, not its behavior. This is a hard antipattern, banned outright.
Any test that reads a .py, .ts, .tsx, etc., file is suspect.

**Why it's actively harmful, not just weak:**

- It passes when the implementation is subtly broken (the regex matches a
  call site that exists but is wired wrong) and fails when a correct
  refactor changes formatting, variable names, or control flow with
  identical runtime behavior. Both directions of failure are wrong.
- It can't be run against a built/bundled/minified artifact, so it silently
  stops testing anything the moment code moves, gets renamed, or a
  dependency reformats it.
- It actively blocks refactors: reviewers see "keeps a pattern intact" tests
  fail during pure structural cleanup with no behavior change, and either
  hand-wave the failure (dangerous) or waste time updating regexes that add
  nothing (waste).
- It gives false confidence. a green suite full of source-regex tests
  looks like coverage but has never once executed the code path it claims
  to guard.

**Do not write:**

```ts
const source = fs.readFileSync(path.join(__dirname, 'main.ts'), 'utf8')

test('backend spawn hides the Windows console', () => {
  assert.match(source, /spawn\(\s*backend\.command,\s*backend\.args[\s\S]{0,300}hiddenWindowsChildOptions/)
})
```

**Do write — extract the logic into a small pure/DI-testable function and
call it for real:**

```ts
// backend-spawn.ts
export function hiddenWindowsChildOptions(options: SpawnOptionsLike = {}, isWindows = process.platform === 'win32') {
  if (!isWindows || 'windowsHide' in options) return options
  return { ...options, windowsHide: true }
}

// backend-spawn.test.ts
test('windowsHide defaults to true on Windows, is left alone elsewhere', () => {
  assert.equal(hiddenWindowsChildOptions({}, true).windowsHide, true)
  assert.equal(hiddenWindowsChildOptions({}, false).windowsHide, undefined)
  assert.equal(hiddenWindowsChildOptions({ windowsHide: false }, true).windowsHide, false)
})
```

If the logic lives inline in a god-file (`main.ts`, `cli.py`,
`gateway/run.py`) and extracting it feels disruptive: that's the actual
signal to do the extraction, not to regex around it.

## Learned User Preferences

- Hermes系再起動は、デスクトップ再ビルド（`hermes desktop --build-only --force-build`）＋ llama ホットスタンバイ（`-StartLlama`）。この経路では `--skip-build` / worktree 起動を使わない。
- Desktop は canonical の packaged `apps/desktop/release/win-unpacked/Hermes.exe`（または同等の release 成果物）から起動する。`.worktrees\...` 上の `npx electron` / `--source --skip-build` は失敗パターンとして避ける。
- User 環境の `HERMES_DESKTOP_HERMES_ROOT` は canonical リポジトリルートのみ。worktree パスを指させない。
- 公式（upstream）向け PR は King's English。`_docs/` と fork 専用面は含めない。
- main への push では環境変数・秘密ファイルをコミットしない。`_docs/` もリモートに載せない。
- スキン壁紙（`background_image`）を有効にするときは、チャット本文が読めるコントラスト／オーバーレイを必須とする。

## Learned Workspace Facts

- Windows の正本チェックアウトは `C:\Users\downl\Documents\New project\hermes-agent` の `main`。Desktop/llama 再起動の RepoRoot に `.worktrees\main-ci-harness-uv` 等を使わない。
- Remotes: `origin` = zapabob/hermes-agent、`upstream` = NousResearch/hermes-agent。
- Desktop/llama 再起動の運用入口は `fork/operations/AGENTS.md` と `scripts/windows/`（例: `restart-hermes-stack.ps1`）。
- ローカル llama サーバーをカスタムプロバイダとして使う想定。このマシンではコンテキスト長 131072 を希望することがある。
