# TypeScript and User Interface Architecture

This reference is required when working in the areas it covers. Root `AGENTS.md` remains authoritative.

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
