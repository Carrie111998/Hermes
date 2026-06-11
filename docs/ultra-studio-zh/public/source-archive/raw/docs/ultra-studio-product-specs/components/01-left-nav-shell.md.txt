# Left Nav Shell

Status: partial — the generic Hermes dashboard sidebar is implemented; all seven
Ultra Studio nav entries are spec-only.
Date: 2026-06-10

Sources:

- Docs: `docs/ultra-studio-product-specs/00-index.md`,
  `01-product-surface.md` (§Left Nav Shell, §Information Architecture, §Non-Goals),
  `02-agent-runtime-contract.md` (§Session Lifecycle),
  `05-memory-marketplace-files.md`, `06-delivery-plan.md` (P0 item 2),
  `docs/hermes-real-chat-agent-ui.md`
- Code: `web/src/App.tsx`, `web/src/plugins/` (`types.ts`, `usePlugins.ts`,
  `slots.ts`), `web/src/components/SidebarStatusStrip.tsx`,
  `web/src/components/AuthWidget.tsx`, `web/src/components/SidebarFooter.tsx`,
  `web/src/hooks/useSidebarStatus.ts`, `web/src/lib/api.ts`,
  `web/src/lib/dashboard-flags.ts`, `hermes_cli/web_server.py`

## Purpose & Scope

The Left Nav Shell is the persistent left rail of Ultra Studio. Per
`01-product-surface.md`, it is "not a decorative sidebar"; it is how users reach
product state that does not fit inside a single chat transcript. The required
Ultra Studio entries are: New task, Search, My office, Marketplace, Files,
Memory, Tasks. `00-index.md` lists the left nav shell as the first element of
the product shape stack, and its top-level acceptance requires Memory,
Marketplace, Files, and Tasks to exist as first-class navigation surfaces.

This spec covers the shell itself: the rail, its nav entries, routing, the
System block, auth widget, footer, mobile behavior, plugin extensibility, and
the contracts each nav entry depends on. It does not specify the full internals
of the destination pages (Marketplace, Files, Memory, Tasks are owned by
`05-memory-marketplace-files.md`; chat/runtime behavior by
`02-agent-runtime-contract.md`).

What exists today is the generic Hermes dashboard sidebar in `web/src/App.tsx`:
a fixed 264px (`w-64`) rail with admin nav, a manifest-driven plugin tab system,
a System block, AuthWidget, theme/language switchers, and a version footer.
None of the seven Ultra Studio entries exist in code (`rg -il
'marketplace|my office'` over `web/src`, `gateway/`, `plugins/` returns zero
hits). The delivery plan path (`06-delivery-plan.md` P0 item 2) is: "Left nav
shell with Tasks, Files, Memory, Marketplace placeholders."

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Fixed left sidebar shell: sticky `w-64` desktop column, off-canvas mobile drawer, brand header, scrollable nav, System block, theme/language row, AuthWidget, footer | `web/src/App.tsx` (`<aside id="app-sidebar">`) |
| Implemented | Built-in admin nav: Sessions, Analytics (config-gated), Models, Logs, Cron, Skills, Plugins, Profiles, Config, Keys (`/env`), Docs; Chat prepended only when embedded chat is enabled | `web/src/App.tsx` (`BUILTIN_NAV_REST`, `CHAT_NAV_ITEM`), `web/src/lib/dashboard-flags.ts` |
| Implemented | Plugin-extensible nav tabs from manifests: position hints, route override, hidden tabs, per-plugin visibility persisted to `config.yaml` | `web/src/plugins/types.ts`, `web/src/App.tsx` (`buildNavItems`/`partitionSidebarNav`), `hermes_cli/web_server.py` |
| Implemented | Plugin bundle loading with optional SRI integrity and load-error states | `web/src/plugins/usePlugins.ts` |
| Implemented | System block: gateway status strip + Restart Gateway / Update Hermes actions | `web/src/components/SidebarStatusStrip.tsx`, `web/src/App.tsx` (`SidebarSystemActions`), `web/src/contexts/SystemActions.tsx` |
| Implemented | AuthWidget, theme/language switchers, version footer | `web/src/components/AuthWidget.tsx`, `web/src/components/SidebarFooter.tsx` |
| Implemented | Sessions page as partial Tasks analog with FTS5 message search | `web/src/pages/SessionsPage.tsx`, `web/src/lib/api.ts` |
| Specified, not built | Nav entries: New task, Search, My office, Marketplace, Files, Memory, Tasks | `01-product-surface.md` §Left Nav Shell |
| Specified, not built | Pricing/account nav entry in the IA tree | `01-product-surface.md` §Information Architecture; pricing copy is out of pack scope per `00-index.md` |
| Specified, not built | Marketplace, Files, Memory, Tasks as browsable surfaces with placeholders at P0 | `05-memory-marketplace-files.md`, `06-delivery-plan.md` P0 item 2 |
| Specified, not built | Cross-surface Search with typed result cards | `05-memory-marketplace-files.md` §Search |
| Specified, not built | Task row click restores transcript, jobs, task files, model, skill profile, memory | `05-memory-marketplace-files.md` §Tasks, `02-agent-runtime-contract.md` §Session Lifecycle |
| Open gap | Build mechanism: built-in routes vs plugin manifests vs separate Ultra shell | no spec chooses; see Open Questions |
| Open gap | Fate of the 12 existing Hermes admin entries in the Ultra Studio shell | specs never mention them |
| Open gap | What a P0 "placeholder" renders | `06-delivery-plan.md` says "placeholders" without a mechanism |
| Open gap | Nav badges / live counts on entries (e.g. running jobs on Tasks) | neither spec nor code defines them |
| Open gap | Ultra Studio branding of the shell | brand is hardcoded "Hermes Agent" in `App.tsx` and i18n `t.app.brand` |

Nothing in the "Specified, not built" rows is shipped. There are no
Tasks/Files/Memory/Marketplace/Search/Office pages in `web/src/pages` today.

## User Entry Points

Implemented today:

| Entry | Behavior | Source |
|---|---|---|
| Browser → dashboard web UI | `/` redirects to `/sessions` (`RootRedirect`); unknown routes also fall back to `/sessions` after plugins finish loading | `web/src/App.tsx` |
| Desktop (≥1024px) | Persistent left sidebar | `web/src/App.tsx` |
| Mobile (<1024px) | Hamburger in a fixed top header opens an off-canvas drawer | `web/src/App.tsx` |
| `/chat` nav entry | Appears only when the server injects `window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true` (`hermes dashboard --tui`) | `web/src/lib/dashboard-flags.ts` |
| Plugin tabs | Appear automatically when a plugin ships `dashboard/manifest.json` (e.g. `plugins/kanban`) | `web/src/plugins/usePlugins.ts` |
| Deep links | Every nav path is a route | `web/src/App.tsx` |
| Status strip / system actions | Click navigates to `/sessions` | `web/src/App.tsx` |

Specified, not built (all planned, `01-product-surface.md`):

| Entry | Intent |
|---|---|
| New task | Start a new creative session |
| Search | Cross-surface search over messages, tasks, files, assets, memory, marketplace entries |
| My office | Workspace home: recent work, shared projects |
| Marketplace | Browse skill/recipe/template/element-pack/character-pack catalog |
| Files | Browse uploaded originals, task files, generated artifacts |
| Memory | Inspect, edit, revoke memory entries |
| Tasks | Recent sessions/projects/jobs; the "Continue work" job ("Open the previous cat task") |
| Pricing/account | In the IA tree; copy out of pack scope |

## Feature List

| Feature | Status | Source |
|---|---|---|
| Fixed left sidebar shell (desktop sticky, mobile off-canvas drawer, Escape close, scroll lock, backdrop) | Implemented | `web/src/App.tsx` |
| Built-in nav: Chat (gated), Sessions, Analytics (config-gated), Models, Logs, Cron, Skills, Plugins, Profiles, Config, Keys, Documentation | Implemented | `web/src/App.tsx` |
| Active-route highlight + i18n nav labels (17 locales) | Implemented | `web/src/App.tsx`, `web/src/i18n/` |
| Plugin nav tabs with position hints (`end`/`after:`/`before:`), route override, hidden tabs, per-plugin visibility toggle persisted to `config.yaml` | Implemented | `web/src/plugins/*`, `hermes_cli/web_server.py` |
| Plugin bundle loading with optional SRI integrity and load-error states | Implemented | `web/src/plugins/usePlugins.ts` |
| System block: gateway status strip (4 states + tone) and active-session count, 10s `/api/status` poll | Implemented | `web/src/components/SidebarStatusStrip.tsx`, `web/src/hooks/useSidebarStatus.ts` |
| System actions: Restart Gateway, Update Hermes with pending/running/disabled states | Implemented | `web/src/App.tsx`, `web/src/contexts/SystemActions.tsx` |
| AuthWidget (gated mode "logged in as" + logout; hidden in loopback) | Implemented | `web/src/components/AuthWidget.tsx` |
| Theme + language switchers and version/org footer | Implemented | `web/src/App.tsx`, `web/src/components/SidebarFooter.tsx` |
| Shell plugin slots near nav: `header-left`, `header-right`, `header-banner`, `backdrop`, `overlay`, cockpit `sidebar` rail | Implemented | `web/src/plugins/slots.ts` |
| Sessions page as partial Tasks analog: list, active badge, msg/tool counts, FTS5 message search, resume-in-chat, delete | Implemented | `web/src/pages/SessionsPage.tsx` |
| "New task" nav entry starting a new creative session | Planned | `01-product-surface.md` §Left Nav Shell; no code |
| Global "Search" entry across sessions/files/assets/memory/marketplace with typed result cards | Planned | `01-product-surface.md`, `05-memory-marketplace-files.md` §Search; only session-scoped FTS5 exists |
| "My office" workspace home (recent work, shared projects) | Planned | `01-product-surface.md`; no code, no backing API |
| "Marketplace" surface with installed/available/disabled/deprecated status | Planned | `05-memory-marketplace-files.md` §Marketplace; nearest machinery is `/plugins` + `/api/dashboard/plugins/hub` and Skills page |
| "Files" surface (uploaded originals, task files, generated artifacts; promote-to-asset) | Planned | `05-memory-marketplace-files.md` §Files; only `POST /api/chat/uploads` exists |
| "Memory" surface (visible/editable/revocable entries, user vs inferred provenance) | Planned | `05-memory-marketplace-files.md` §Memory; `plugins/memory` agent plugin has no dashboard manifest |
| "Tasks" surface with job-aware rows (status, active jobs, output count, source) and full restore | Planned | `05-memory-marketplace-files.md` §Tasks, `02-agent-runtime-contract.md`; `SessionInfo` lacks the fields |
| "Pricing / account" nav entry | Planned | `01-product-surface.md` §Information Architecture; copy out of pack scope per `00-index.md` |
| Ultra Studio branding of the shell | Planned | current brand hardcoded "Hermes Agent" in `App.tsx` and i18n |
| Decision: built-in routes vs dashboard plugins vs separate shell for the seven entries | Gap | specs are silent; P0 says "placeholders" without a mechanism |
| Placeholder/empty-state definition for Tasks/Files/Memory/Marketplace at P0 | Gap | `06-delivery-plan.md` P0 item 2 |
| Whether admin entries (Logs, Cron, Config, Keys, Profiles…) stay visible to Ultra Studio end users | Gap | specs never mention them |
| Badge/notification affordances on nav entries | Gap | neither spec nor code |

## State Machine

The shell itself holds little state; its state machines are the small loops
below. All "Implemented" machines are in code today.

### Gateway state (Implemented)

Rendered by `SidebarStatusStrip` from `StatusResponse.gateway_state`:

```text
running | starting | startup_failed | stopped
```

Fallback when `gateway_state` is null: `gateway_running` boolean → running/off.
Transitions are driven by the backend; the shell only displays them.

### Sidebar status fetch (Implemented)

```text
null (skeleton) -> StatusResponse
```

`useSidebarStatus.ts` polls `GET /api/status` every 10s. Errors keep the last
value silently — the strip goes stale with no UI signal. Trigger: timer.

### System action lifecycle (Implemented)

Per action (`restart` | `update`), in `SidebarSystemActions` +
`contexts/SystemActions.tsx`:

```text
idle -> pending (user click, spinner)
     -> running (activeAction + isRunning, runningLabel)
     -> finished | failed
```

The sibling action is disabled while one is busy. Trigger: user click; progress
polled via `GET /api/actions/{name}/status`.

### Plugin load, per manifest (Implemented)

```text
manifest fetched -> script injected -> registered
                                    | LOAD_FAILED
                                    | NO_REGISTER
```

Global loading flag clears on all-registered or a 2s timeout
(`web/src/plugins/usePlugins.ts`, `registry.ts`). Trigger: shell mount.

### Mobile drawer (Implemented)

```text
closed <-> open
```

Open: hamburger click. Close: backdrop click, Escape, nav-link click, or
viewport resize to ≥1024px. Trigger: user input / matchMedia listener.

### Task/session lifecycle relevant to the Tasks entry

Implemented today: only `is_active` true/false on `SessionInfo`. The spec task
states — a `status` field, active jobs, running/completed creative jobs — are
Planned (`05-memory-marketplace-files.md` §Tasks; job states live in
`03-media-asset-contract.md`).

### Marketplace item status (Planned)

```text
installed | available | disabled | deprecated
```

From the `05-memory-marketplace-files.md` §Marketplace item yaml. Transitions
(install, enable, disable, deprecate) and their triggers are not yet specified;
mapping onto the existing plugin enable/disable endpoints is an open question.

## APIs & Events

### Implemented (in code today)

| API | Use by the shell | Source |
|---|---|---|
| `GET /api/status` → `StatusResponse` | Status strip + footer version; 10s poll | `web/src/lib/api.ts:155`, `hermes_cli/web_server.py:569` |
| `GET /api/config` | Reads `dashboard.show_token_analytics` to gate the Analytics nav entry | `web/src/App.tsx` |
| `GET /api/dashboard/plugins` → `PluginManifest[]` | Plugin nav tabs; filters `dashboard.hidden_plugins` | `hermes_cli/web_server.py:4388` |
| `GET /api/dashboard/plugins/rescan`, `GET /api/dashboard/plugins/hub`, `POST /api/dashboard/plugins/{name}/visibility`, plugin install/enable/disable/update/delete | Plugin management feeding nav visibility | `hermes_cli/web_server.py:4403-4673` |
| `GET /dashboard-plugins/{plugin}/{file}` | Plugin JS/CSS asset serving | `hermes_cli/web_server.py:4673` |
| `POST /api/actions/...`, `GET /api/actions/{name}/status?lines=` | Restart/update progress | `web/src/lib/api.ts:~387` |
| `GET /api/auth/me`, `GET /api/auth/providers`, `POST /auth/logout` | AuthWidget | `web/src/components/AuthWidget.tsx` |
| `GET /api/sessions?limit&offset`, `GET /api/sessions/{id}/messages`, `DELETE /api/sessions/{id}`, FTS5 `searchSessions` | Sessions page; feeds the future Tasks surface | `web/src/lib/api.ts:177-321` |
| Gateway WS `/api/ws` (`session.create`/`session.resume`, `prompt.submit`, `slash.exec`) + `/api/events?channel=` fanout | Used by chat, not by the nav shell itself | `web/src/components/ChatSidebar.tsx`, `docs/hermes-real-chat-agent-ui.md` |

### Proposed (no code; required by the planned entries)

None of these endpoints exist. Shapes are unspecified unless noted.

| Surface | Needed contract | Spec source |
|---|---|---|
| Search | Cross-surface search API over messages, tasks, files, assets, memory, marketplace entries; per-type result card schema; ranking | `05-memory-marketplace-files.md` §Search |
| Tasks | Task listing with status, active jobs, output count, last user request, source — via `SessionInfo` extension or a new tasks API joined with media jobs | `05-memory-marketplace-files.md` §Tasks |
| Tasks restore | `session.resume` restoring messages, active jobs, selected assets, task files (gateway contract exists on paper; web client does not implement job/asset restoration) | `02-agent-runtime-contract.md` §Session Lifecycle |
| Files | Listing, preview, promote-to-asset API; must never list skill-internal `references/` | `05-memory-marketplace-files.md` §Files, `docs/hermes-real-chat-agent-ui.md` §References |
| Memory | List/edit/revoke API with provenance (source session, user vs inferred) | `05-memory-marketplace-files.md` §Memory |
| Marketplace | Catalog API (local checked-in catalog vs server catalog is an explicit open question) | `05-memory-marketplace-files.md` §Marketplace, `06-delivery-plan.md` §Open Questions |
| My office | Workspace/project entities and APIs; nothing exists in any layer | `01-product-surface.md` |

## Data Model

### Implemented entities

| Entity | Fields | Persisted where |
|---|---|---|
| `NavItem` (`web/src/App.tsx`) | `path: string`, `label: string`, `labelKey?: string`, `icon: ComponentType` | In-memory only (built per render) |
| `PluginManifest` (`web/src/plugins/types.ts`) | `name`, `label`, `description`, `icon` (resolved via `ICON_MAP`, fallback Puzzle), `version`, `tab { path, position?: 'end' \| 'after:<seg>' \| 'before:<seg>', override?, hidden? }`, `slots?`, `entry`, `css?`, `has_api`, `integrity?`, `source` | `plugins/<name>/dashboard/manifest.json` on disk; served by the backend |
| `StatusResponse` (`web/src/lib/api.ts`) | `active_sessions: number`, `auth_required?`, `auth_providers?: string[]`, `gateway_state: string \| null`, `gateway_running: boolean`, `gateway_pid`, `gateway_exit_reason`, `gateway_health_url`, `gateway_platforms: Record<string, PlatformStatus>`, `gateway_updated_at`, `version`, `release_date`, `hermes_home`, `config_path`, `config_version`, `latest_config_version` | Computed by the backend per request |
| `SessionInfo` (`web/src/lib/api.ts`) | `id`, `source: string \| null`, `model`, `title`, `started_at`, `ended_at`, `last_active`, `is_active`, `message_count`, `tool_call_count`, `input_tokens`, `output_tokens`, `preview`, `parent_session_id?` | Backend session store (FTS5-indexed messages) |
| `SystemAction` (`web/src/contexts/system-actions-context.ts`) | `'restart' \| 'update'` | In-memory |
| Plugin visibility | `dashboard.hidden_plugins` list | `config.yaml` |

`SessionInfo` is missing the spec task-row fields: status, active jobs, output
count, last user request.

### Planned entities (no code; fields from `05-memory-marketplace-files.md`)

| Entity | Fields | Notes |
|---|---|---|
| Marketplace item | `id`, `kind (skill \| recipe \| template \| element_pack \| character_pack)`, `title`, `description`, `category`, `inputs_schema`, `output_type`, `required_tools`, `provider_constraints`, `version`, `status (installed \| available \| disabled \| deprecated)` | v1 may be a local catalog of checked-in skill metadata |
| Memory entry | Implied, not formalized: category (user preferences, brand rules, project facts, prompt decisions, model preferences, rejected styles, safety notes), scope (user/workspace/project), source session, user-authored vs inferred flag | Must never store provider secrets; schema must be defined |
| Task row | `title`, session id, last user request, `status`, active jobs, output count, date, `source (web \| tui \| cli \| panel)` | Storage decision pending (extend `SessionInfo` vs new aggregate) |
| File entry | category (uploaded original, downloaded web artifact, generated task file, log, prompt plan, storyboard sheet, rendered output), promotable-to-asset flag | Field types undefined; promotion is explicit, never automatic |

## UI Behavior

Implemented (all in `web/src/App.tsx` unless noted):

- Sidebar is `w-64`, full-height flex column: brand header (`h-14`) →
  scrollable nav (core items, then a "Plugins" group with heading) → "System"
  group (status strip + restart/update) → theme/language row → AuthWidget →
  footer.
- `SidebarNavLink` is a react-router `NavLink`. Active state: midground text +
  1px left active bar (mix-blend plus-lighter). Hover overlay at 5% opacity,
  focus-visible ring, uppercase mondwest display font, truncated labels. Labels
  resolve via i18n `t.app.nav` (17 locale files).
- Plugin tabs honor `after:`/`before:`/`end` position hints and may override
  built-in routes. Overriding `/chat` suppresses the persistent embedded chat
  host; a `pluginsLoading` gate avoids killing the PTY mid-paint.
- Status strip: skeleton pulse while status is null; gateway label color-coded
  (success/warning/destructive/muted); the whole strip is a Link to `/sessions`
  with a title tooltip (`SidebarStatusStrip.tsx`).
- System actions: spinner while pending/running, label swap (e.g. "Restarting
  gateway…"), sibling action disabled while one is busy; click navigates to
  `/sessions` and closes the mobile drawer.
- Mobile: fixed top header with hamburger (`aria-expanded`, `aria-controls
  app-sidebar`); 200ms translate drawer animation; backdrop blur button; Escape
  close; body overflow lock; auto-close at ≥1024px.
- Accessibility present: `aria-label` on nav/aside, `aria-busy` on actions,
  `role=group` + `aria-labelledby` on the plugin section.
- Analytics is hidden from nav when the config flag is off but stays
  URL-reachable with an explanation page (`pages/AnalyticsPage.tsx`).

Planned (spec requirements on the future shell):

- The nav is the primary access path to durable workspace state, not a
  decorative sidebar (`01-product-surface.md`).
- Search results are typed by surface: "a memory should not look like a file"
  (`05-memory-marketplace-files.md` §Search).
- Clicking a Task row restores the full working context: transcript,
  active/complete jobs, task files, selected model, active skill profile,
  relevant memory (`05-memory-marketplace-files.md` §Tasks).
- Nothing renders from fake data; placeholder surfaces must not fake run/status
  panels (`06-delivery-plan.md` launch gates, `01-product-surface.md`
  §Non-Goals).

## Permissions & Error Handling

Implemented:

- Auth is binary today: OAuth gate (`auth_required`) vs loopback/`--insecure`.
  AuthWidget shows the user id (truncated to 14 chars) + logout in gated mode
  and renders nothing in loopback. A 401 on `/api/auth/me` hides the widget; a
  network error shows "auth status unavailable" (`AuthWidget.tsx`).
- No per-nav-entry permissions exist. Plugin visibility is user config
  (`dashboard.hidden_plugins` in `config.yaml`), not permission-based.
- Plugin load failure: per-plugin `LOAD_FAILED` / `NO_REGISTER` states with
  i18n messages shown on the plugin page; network failures `console.warn`.
- SRI integrity is optional on plugin bundles; without it, tampered delivery
  executes silently (documented trade-off in `usePlugins.ts`).
- Known silent-degradation spots, candidate violations of the "real blockers
  visible" launch gate: `useSidebarStatus` catches fetch errors with no UI
  signal (the strip goes stale), and `getConfig` failure silently defaults
  Analytics off (`web/src/hooks/useSidebarStatus.ts`, `web/src/App.tsx`).

Planned constraints:

- `05-memory-marketplace-files.md` §Access Control requires permissions
  read/use/update/delete/revoke/share per surface; Marketplace items visible
  without being enabled; Memory scoped user/workspace/project; Files scoped
  session/project; shared conversations must not imply shared sandbox or
  credentials. No such model exists in code.
- Skill-internal `references/` must never appear in any file browser or export
  UI (`docs/hermes-real-chat-agent-ui.md` §References). This is a hard
  constraint on the future Files surface.
- Per-surface error contracts are required: stale or unavailable backing data
  must be visible in the shell, not silently swallowed.

## Acceptance Criteria

Implemented behavior (verifiable today):

1. Loading `/` in the dashboard redirects to `/sessions`; an unknown URL
   redirects to `/sessions` after plugins finish loading.
2. At <1024px the sidebar renders as a drawer: hamburger opens it, Escape and
   backdrop close it, body scroll is locked while open, and resizing to
   ≥1024px auto-closes it.
3. A plugin shipping `dashboard/manifest.json` with `tab.path` appears in the
   nav under the "Plugins" group at the position its hint requests (verify
   with `plugins/kanban`: `/kanban`, `after:skills`).
4. With `dashboard.show_token_analytics` false (default), Analytics is absent
   from the nav but `/analytics` is still URL-reachable.
5. `/chat` appears in the nav only when launched as `hermes dashboard --tui`.
6. The status strip reflects `gateway_state` from `GET /api/status` within one
   10s poll cycle, and shows a skeleton before the first response.

Target behavior for the Ultra Studio shell (testable once built; currently all
fail):

7. The nav exposes New task, Search, My office, Marketplace, Files, Memory,
   and Tasks as entries; Marketplace, Files, Memory, and Tasks route to real
   surfaces (placeholders acceptable at P0, per `06-delivery-plan.md`).
8. No placeholder surface renders fake data: a P0 placeholder shows an
   explicit empty/coming-soon state, never invented rows, counts, or statuses.
9. A Tasks row shows title, status, active jobs, output count, date, and
   source; clicking it restores transcript, jobs, task files, model, and skill
   profile per `02-agent-runtime-contract.md`.
10. Search returns typed result cards; a memory result is visually and
    structurally distinct from a file result.
11. Marketplace lists installed and disabled items, not only available ones.
12. Memory entries are inspectable and revocable from the Memory surface, and
    each shows its source session and user-authored vs inferred provenance.
13. The Files surface never lists skill-internal `references/` paths.
14. A failure to load the data behind any nav surface produces a visible error
    state in that surface, not a stale or silently empty view.

## Non-Goals

- Do not merge Marketplace, Memory, and Files into one generic "Assets" page
  (`01-product-surface.md` §Non-Goals).
- No raw provider dashboards inside the shell (`01-product-surface.md`
  §Non-Goals).
- No fake run/status panel disconnected from Hermes events
  (`01-product-surface.md` §Non-Goals).
- This spec does not define pricing/account page content; pricing copy is out
  of pack scope (`00-index.md`). Only the nav entry's existence is in
  question (see Open Questions).
- This spec does not define the internal page contracts of Marketplace, Files,
  Memory, Tasks (owned by `05-memory-marketplace-files.md`) or media job
  states (owned by `03-media-asset-contract.md`).
- No public app store at v1: Marketplace may start as a local catalog of
  checked-in skill metadata (`05-memory-marketplace-files.md` §Marketplace).

## Open Questions

1. Build mechanism: extend `BUILTIN_ROUTES_CORE`/`BUILTIN_NAV_REST` in
   `App.tsx`, ship the seven entries as dashboard plugins (the manifest system
   already supports position/override/hidden), or fork a separate Ultra shell?
   The specs do not choose.
2. Do the 12 existing Hermes admin nav entries stay, get grouped under an
   admin section, or get hidden for Ultra Studio users — and by what gate
   (config flag like `show_token_analytics`, profile, or auth role)?
3. What exactly is a P0 "placeholder" for Tasks/Files/Memory/Marketplace —
   empty routed page, disabled nav item, or coming-soon card? Launch gates
   forbid fake data, so placeholder content rules matter.
4. "New task": create a session immediately via gateway `session.create`, or
   navigate to an empty composer? How does it interact with the embedded-chat
   flag, which today requires `hermes dashboard --tui`? Ultra Studio
   presumably needs the chat surface without the TUI flag.
5. Search form factor (command palette vs page) and API: extend FTS5 session
   search or build a new typed cross-surface index? What are the result-card
   schemas per type?
6. "My office": what entities back "workspace / shared projects"? No workspace
   or project model exists anywhere in the codebase.
7. Tasks: extend `SessionInfo` (+`status`, `active_jobs`, `output_count`,
   `last_user_request`) or introduce a separate task aggregate joining
   sessions with media jobs from `03-media-asset-contract.md`?
8. Marketplace MVP source: local checked-in catalog vs server catalog
   (explicit open question in `06-delivery-plan.md`); reuse
   `/api/dashboard/plugins/hub` + skills machinery or build a new catalog
   service? How does `installed | available | disabled | deprecated` map onto
   existing plugin enable/disable endpoints?
9. Memory default scope: project-scoped vs user-scoped with project filters
   (explicit open question in `06-delivery-plan.md`); where does the dashboard
   read memory from? `plugins/memory` is an agent plugin with no dashboard
   API.
10. Do Task Files and the Asset Library share storage keys or separate buckets
    (`06-delivery-plan.md` open question), and which existing store
    (`dashboard-uploads/`, sandbox) backs the Files page?
11. Is "Pricing / account" in scope for this component at all? `00-index.md`
    lists pricing copy as out of scope, but the IA tree includes the entry.
12. Should nav entries show live badges (running job counts, unread
    approvals), and should they be event-driven via `/api/events` instead of
    the current 10s `/api/status` poll?
13. Branding: how does the shell switch from "Hermes Agent" to Ultra Studio
    branding — theme `layoutVariant`, i18n brand key, or build-time flag?
14. Task restore failure behavior: `02-agent-runtime-contract.md` defines what
    `session.resume` restores, but partial-restore and missing-sandbox
    behavior is unspecified, and the web client implements none of the
    job/asset restoration today.
15. Mobile IA: `01-product-surface.md` specifies a three-pane layout, but no
    responsive behavior is defined for 7+ nav entries plus the inspector on
    small screens; current code only handles the nav drawer.
