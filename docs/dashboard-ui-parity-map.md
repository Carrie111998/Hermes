# Hermes Dashboard UI Parity Map

This map is grounded in the production React route table, the canonical dashboard page manifest, and a live authenticated crawl of `http://127.0.0.1:9119` at 1440×1000. The crawl covered built-in, secondary, and dynamically registered plugin routes.

## Navigation graph

```mermaid
flowchart LR
  shell[Hermes Studio shell]
  primary[Left primary navigation]
  context[Far-right contextual navigation]
  shell --> primary
  shell --> context

  primary --> workspace[Workspace]
  workspace --> chat[/chat Chat]
  workspace --> sessions[/sessions Sessions]
  workspace --> files[/files Files]
  workspace -. capability gated .-> analytics[/analytics Analytics]

  primary --> automation[Automation]
  automation --> cron[/cron Scheduled jobs]
  automation --> webhooks[/webhooks Webhooks]
  context --> automation

  primary --> integrations[Integrations]
  integrations --> channels[/channels Channels]
  integrations --> mcp[/mcp MCP]
  integrations --> pairing[/pairing Pairing]
  context --> integrations

  primary --> manage[Manage]
  manage --> models[/models Models]
  manage --> logs[/logs Logs]
  manage --> skills[/skills Skills]
  manage --> plugins[/plugins Plugins]
  manage --> profiles[/profiles Profiles]
  profiles --> profileNew[/profiles/new New profile]
  manage --> config[/config Configuration]
  manage --> env[/env Keys]
  manage --> system[/system System]
  manage --> docs[/docs Documentation]
  context --> manage

  primary --> extensions[Plugin pages]
  extensions --> kanban[/kanban Kanban]
  extensions --> achievements[/achievements Achievements]
```

### Contextual-navigation rules

- Workspace pages do not render a contextual rail. They are top-level destinations, and Chat already owns a right-side model/tools inspector.
- Automation, Integrations, Manage, and Extensions render sibling pages in the far-right rail at `xl` and above. Below `xl` they use the accessible right-side drawer so 1024px layouts retain enough workspace width.
- `/profiles/new` is a secondary route and inherits the Manage context through the `/profiles` prefix.
- Active, visible, non-overriding plugin pages participate in the canonical manifest under the `extensions` group. Two or more extension pages produce their own contextual rail/drawer group.

## Route, field/action, and API matrix

Shell-wide API calls (`/api/status`, `/api/config`, profile scope, dashboard theme/font, plugin manifest, and page manifest) are omitted below so the table shows page-specific dependencies.

| Route | Navigation state | Visible fields/actions in default live state | Page-specific live API paths | Canonical MCP page |
|---|---|---|---|---|
| `/sessions` | Primary; Workspace; no context rail | Source tabs (Chats/Automation/All), source filter, Overview/History, prune, import, session list actions | `/api/sessions`, `/api/sessions/stats`, `/api/sessions/empty/count` | Yes |
| `/chat` | Primary when embedded chat is enabled; Workspace; no context rail | Browser-style session tabs with independently persistent xterm chats, model/tools inspector, copy last response; image paste/upload and PTY controls are runtime-driven | `/api/model/info`, `/api/sessions`, PTY WebSocket, `/api/pty/terminate` | Yes |
| `/files` | Primary; Workspace; no context rail | Path, Go, refresh, upload, create folder, open/download/delete entries, preview/editor dialogs | `/api/files` plus read/upload/mkdir/delete actions | Yes |
| `/analytics` | Conditional primary item (`showTokenAnalytics`); Workspace | Hidden-state explanation when token analytics are unavailable; analytics filters/charts when supported | Provider/session analytics hooks | Yes, but availability is not expressed |
| `/models` | Primary; Manage context | Time range, refresh, provider/model cards, Configure, Change, Use as, auxiliary/MoA assignment controls | `/api/model/moa`, `/api/analytics/models`, `/api/model/auxiliary` | Yes |
| `/logs` | Primary; Manage context | Refresh, log source, severity/source filters, row limit (50/100/200/500) | `/api/logs` | Yes |
| `/cron` | Primary; Automation context | Create, Jobs/Blueprints, profile scope, job editor and lifecycle actions | `/api/cron/jobs`, `/api/cron/delivery-targets`, `/api/model/options`, `/api/skills`, `/api/tools/toolsets` | Yes |
| `/skills` | Primary; Manage context | Search, category/source filters, Learn a skill, New skill, enable/disable/update actions | `/api/skills`, `/api/tools/toolsets` | Yes |
| `/plugins` | Primary; Manage context | Runtime provider selectors, rescan, install URL, install/update/enable/disable/remove, visibility/config controls | `/api/dashboard/plugins/hub` and plugin-management actions | Yes |
| `/mcp` | Primary; Integrations context | Add server, enable/disable, authenticate, test, delete, catalog install | `/api/mcp/servers`, `/api/mcp/catalog` | Yes |
| `/channels` | Primary; Integrations context | Restart gateway; per-platform enable/configure/test/onboarding controls for all available channel adapters | `/api/messaging/platforms` and onboarding/action endpoints | Yes |
| `/pairing` | Primary; Integrations context | Pending and approved users, approve/revoke, clear pending | `/api/pairing` | Yes |
| `/webhooks` | Primary; Automation context | New subscription, global enable, per-subscription enable/delete, gateway restart/action status | `/api/webhooks` | Yes |
| `/system` | Primary; Manage context | Updates, portal, curator, gateway, memory reset, credential pool, operations, checkpoints, hooks, backup/restore, doctor/audit/support tools | `/api/system/stats`, `/api/memory`, `/api/ops/hooks`, `/api/credentials/pool`, `/api/ops/checkpoints`, `/api/curator`, `/api/portal`, `/api/hermes/update/check` | Yes |
| `/profiles` | Primary; Manage context | Build/Create, profile actions, activate, model, description/soul, rename/delete/setup | Profile management endpoints | Yes |
| `/profiles/new` | Secondary from Profiles; inherits Manage context | Identity, Model, Skills, MCPs, Review steps; name/purpose fields; Back/Next/Create | Profile creation and option catalogs | No (intentional secondary route) |
| `/config` | Primary; Manage context | Search, schema-driven fields, raw YAML, export/import JSON, reset section, save | `/api/config/schema`, `/api/config/defaults`, `/api/config/raw` | Yes |
| `/env` | Primary as Keys; Manage context | OAuth/Providers/Tools/Gateway/Settings/Custom Keys tabs; reveal/set/replace/clear; provider login/disconnect | `/api/env`, `/api/providers/oauth` | Yes |
| `/docs` | Primary; Manage context | Embedded official documentation and Open Documentation external action | External docs iframe; no page-specific local API | Yes |
| `/kanban` | Dynamic plugin primary item; Extensions context | Board selector/new board/settings, orchestration controls, filters, tenant/profile selectors, dispatcher nudge, refresh, columns and task actions | `/api/plugins/kanban/config`, `/board`, `/orchestration`, `/profiles`, `/boards` | Yes (`plugin-kanban`) |
| `/achievements` | Dynamic plugin primary item; Extensions context | Rescan, category filters, locked/unlocked/discovered/secret status filters | `/api/plugins/hermes-achievements/achievements` | Yes (`plugin-hermes-achievements`) |

## Parity status

### Preserved and wired

- Existing page components remain authoritative; the Studio work changed the shell and navigation rather than replacing page forms or APIs.
- All 21 live routes render under the Studio shell after direct navigation.
- All live route/API requests complete successfully in the current crawl.
- Primary navigation remains on the left.
- Automation, Integrations, and Manage submenus render on the far right and become an accessible mobile drawer.
- Canonical MCP discovery/deep links cover all built-in top-level product pages.
- Canonical REST/focused-MCP/broad-MCP discovery also covers active plugin tabs after strict same-origin route validation.
- `/profiles/new` remains reachable from Profiles and inherits the correct context.
- System, Light, and Dark Studio appearances use the existing theme provider.

### Gaps found and fixed during parity crawl

1. **Files page failed on a dangling removable-volume symlink.** A single inaccessible entry no longer aborts the entire directory listing; it is omitted without modifying the symlink or filesystem.
2. **Direct `/docs` deep links opened FastAPI Swagger instead of React Documentation.** Swagger now lives at `/api/docs`; `/docs` correctly enters the SPA and renders the Manage contextual rail.
3. **Home browsing exposed high-confidence credential trees.** `.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, `.azure`, `.mcp-auth`, `.config/gh`, and `.config/gcloud` are hidden under the user home; both lexical and resolved targets are checked so symlinked credential trees cannot bypass direct read/download denial, while existing empty-list semantics are preserved.
4. **`/profiles/new` used the raw shell title `Profiles/new`.** It now resolves to `New profile` through explicit route metadata.
5. **Built-in React routes and the Python page manifest could drift silently.** A cross-language contract test now compares the declared route table to canonical MCP/REST discovery, with only `/` and `/profiles/new` explicitly excluded.
6. **Plugin routes were visible but absent from canonical discovery/context.** Active plugin tabs now use validated paths, deterministic `plugin-*` IDs, an `extensions` context group, and shared REST/focused-MCP/broad-MCP links. Canonical and React discovery share source-aware activation and deterministic first-owner collision rules; unauthenticated browser asset serving shares the activation policy and fails closed on indeterminate configuration. Explicit overrides must target a known built-in route.
7. **Closed primary mobile navigation remained keyboard-accessible off-screen.** The drawer is now inert and assistive-technology-hidden while closed; while open it has modal semantics, initial focus, Tab/Shift+Tab containment, Escape/backdrop dismissal, and trigger-focus restoration.
8. **The persistent context rail compressed pages at the 1024px breakpoint.** The rail now begins at `xl`; the related-pages trigger and modal drawer remain available from mobile through 1279px, with no horizontal overflow at 1024px or 1280px.
9. **Chat exposed only one persistent browser PTY.** The Chat workspace now supports up to eight accessible browser-style tabs, each with an independent attach token, resume identity, mounted xterm session, and explicit authenticated termination on close. Tabs persist for the browser session, remain alive while inactive, support Arrow/Home/End keyboard navigation, and scroll horizontally without page overflow on narrow screens.

### Open gaps / decisions

| Priority | Gap | Impact | Proposed next action |
|---|---|---|---|
| P2 availability | Analytics remains in the static manifest when its primary menu item is capability-gated off. | MCP can link to a valid page that only explains analytics are unavailable. | Add optional availability metadata rather than removing the route. |
| P2 scale | Manage context currently lists nine sibling pages. | The right rail is complete but long, especially on smaller laptops. | Evaluate sub-group labels or collapsible sections without moving the rail from the far right. |

## Live verification snapshot

- Routes crawled: 21
- Failed HTTP responses: 0
- Browser console errors: 0
- Desktop viewport: 1440×1000
- Responsive navigation checks: 390×844, 1024×800, and 1280×800
- Frontend tests after Chat tabs and mobile/responsive remediation: 214/214
- Dynamic plugin routes discovered from rendered navigation: 2
- Raw sanitized crawl artifact (local, not committed): `/Users/aibot/.hermes/designs/hermes-dashboard-live-inventory.json`
