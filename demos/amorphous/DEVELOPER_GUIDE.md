# Hermes Station — Internal Developer Guide

> **Codename:** Hermes Station · **Concept:** Amorphous Applications
> **Status:** PoC / internal demo · **Owner:** Teknium / Nous Research
> This is the internal spec sheet: every primitive, system, contract, and
> design rule in the build. Read this before touching anything.

---

## 1. What This Is

A per-user/org **mission-control work surface** that is Hermes-powered and
Hermes-shaped. The dashboard is composed from a typed component library,
fed by real datasources, driven by a full `AIAgent` (the user's configured
model + toolsets), instrumented with interaction telemetry, and **reshaped
over time** by an evolution curator with a user approval loop. The thesis:
this becomes the only interaction layer a user needs for their job
("Amorphous Applications" — software with no fixed final form).

```
┌─────────────────────────────────────────────────────────────────┐
│ React SPA (Vite, Tailwind v4, Radix, recharts, react-grid-layout)│
│   sidebar · stats strip · draggable card grid · pop-out dialogs  │
│   invariant chat dock (bottom/right/floating) · inspector rail   │
└──────────────▲──────────────────────────────▲───────────────────┘
        REST + SSE                      telemetry beacons
┌──────────────┴──────────────────────────────┴───────────────────┐
│ FastAPI server (server.py)                                       │
│   layout store · workflows · proposals · curator scheduler       │
│   data watcher (change-hash → SSE push) · client black box       │
└───────┬───────────────┬────────────────┬────────────────────────┘
        │               │                │
   store.py        datasources.py   agent_bridge.py
   (SQLite)        (live connectors) (REAL AIAgent + station_* tools)
```

Runtime: `python demos/amorphous/serve.py --port 8877` (binds 0.0.0.0).
Frontend build: `cd web && npm run build` (served from `web/dist`,
stable asset names, no-store). Dev loop: `npm run dev` (Vite proxy → :8877).

---

## 2. Backend Primitives

### 2.1 Layout Store (`store.py`)
SQLite, 6 tables. Everything is per-`user_id` (multi-tenant by string key).

| Table | Purpose | Key semantics |
|---|---|---|
| `layouts` | **Append-only versioned** dashboard specs | `source` ∈ seed/user/agent/component-agent/curator/rebuild — full audit trail; rollback = re-save an old version |
| `events` | Raw interaction telemetry | types: view, click, focus_dwell{seconds}, hide, move, resize, remove, workflow_run, chat, component_chat, context_menu, component_chat_open, maximize, proposal_action, onboarded |
| `proposals` | Curator mutation sets awaiting decision | `status` ∈ pending/approved/rejected/superseded; one pending at a time (new run supersedes) |
| `workflows` | Reusable agent task templates | `created_by` ∈ seed/user/curator/agent; `prompt_template` with `{context}` + named placeholders |
| `workflow_runs` | Execution history | last runs surface in workflow cards |
| `feedback` | User sentiment on proposals | fed to the curator LLM next run |

Key methods: `save_layout` (bumps version), `get_active_layout` (latest),
`usage_stats(user, since)` (per-component aggregation for the curator),
`rejected_mutations(user, 7d)` (negative guidance), `list_users()` (watcher).

### 2.2 Dashboard Spec (the layout contract)
```jsonc
{
  "title": "…", "user_id": "…", "template": "developer",
  "grid": { "columns": 12 },
  "chat_dock": { "position": "bottom" | "right", "visible": true },
  "components": [{
    "id": "dev-log",            // stable id — telemetry + watcher key on it
    "type": "table",            // one of the 10 library types
    "title": "Commit history",
    "col": 0, "row": 1,          // grid units; PRESERVED verbatim for users
    "w": 6, "h": 3,              // 12-col grid, 96px row height client-side
    "hidden": false,
    "props": {
      "source": "git.log",      // datasource binding (data components)
      "query": { "repo": "~/x", "limit": 10 },
      "refresh_s": 10            // OPTIONAL live-update cadence override
      // workflow components: workflow_id, inputs:[{name,label}]
      // notes: markdown
    }
  }]
}
```
**Placement rule:** components WITH col/row are never moved by the server
(`_reflow` in `components.py` only packs components *missing* coordinates
into free space). User drag positions are sacred; the client grid
(react-grid-layout, vertical compaction) resolves residual overlap.

### 2.3 Component Library (`components.py`)
10 types: `metric, timeseries, table, kv, feed, links, workflow_button,
workflow_panel, notes, connections` — each with min sizes. Server resolves
data per type in `server._component_payload`.

**Mutation engine** — `apply_mutations(spec, [...])`, ops:
`promote, shrink, resize{w,h}, hide, show, remove, retitle{title},
add{component}, set_props{props}, set_notes{markdown},
move_chat_dock{position}` + `replace_spec{spec}` (rebuild proposals only).
Used by: curator proposals, agent station_mutate, approval application.

**Templates** — `developer` (repo-parameterized), `trader`, `executive`,
`blank`; each seeds workflows (`TEMPLATE_WORKFLOWS`) whose prompts instruct
the agent to use its real tools (git diff via terminal, web research, etc.)

### 2.4 Datasources (`datasources.py`)
**Hard rule: no fake data.** Connected → real result; not connected →
`{kind:"unconnected", how:"set X in ~/.hermes/.env"}` rendered as setup
guidance. Never demo numbers.

| Source | Backing | Payload kind |
|---|---|---|
| `git.log` / `git.status` | local `git` subprocess | table / kv |
| `github.prs` / `github.issues` | `gh` CLI (`--json`) | table |
| `system.stats` | /proc + os | kv (usage bars) |
| `crypto.price` / `crypto.chart` | CoinGecko (no key, TTL-cached) | table / timeseries |
| `rss` | any feed URL (stdlib XML) | links |
| `weather` | Open-Meteo (no key) | kv |
| `datadog.query` | Datadog API (env keys) | timeseries |
| `betterstack.monitors` | Better Stack API (env key) | table |
| `station.activity` | own Store (workflows+chat) | feed |

Payload contract (what renderers understand): `metric{value,delta,unit}`,
`kv{pairs}` (auto usage-bars on "x / y" and "n%"), `table{columns,rows}`,
`timeseries{label,points[[ts,v]]}`, `links{links[{title,url}]}`,
`feed{items[{when,icon,text}]}`, `notes{markdown}`, `connections{...}`,
`workflow{workflow,runs}`, `unconnected{source,how}`, `error{error}`.

`detect_connections()` powers onboarding + sidebar + inspector: probes gh
auth, scans for local git repos, reports key-gated sources' requirements.

### 2.5 Agent Bridge (`agent_bridge.py`) — the real thing
No HTTP shim: constructs **`AIAgent` from the hermes-agent repo** with the
user's configured provider/model via `resolve_runtime_provider()` +
`load_cli_config()`. Toolsets: `terminal, file, web, vision, todo, skills,
station`.

**Station toolset** (registered into the live tool registry AFTER
model_tools discovery; added to `_HERMES_CORE_TOOLS` so tool_search never
defers them):

| Tool | Contract |
|---|---|
| `station_get_dashboard` | read layout + workflows (component-scoped: just that component) |
| `station_mutate` | apply mutation list; **applies immediately** (no approval loop for chat-driven edits); documents `refresh_s` cadence |
| `station_query_datasource` | dry-run any source/query — agent verifies data BEFORE wiring a component |
| `station_create_workflow` | mint reusable workflow templates |
| `station_component_data` | exactly what the user currently sees in a card |

**Two session scopes**, enforced **server-side in the tool handler** (not by
prompt): the main dock agent has full layout authority; a per-component
agent (`/api/component/{id}/chat`) may only mutate its own component_id and
cannot add components. Context injection per turn: main gets a slim
dashboard snapshot; component chat gets the component definition + current
rendered data. Thread-local `set_station_context(store, user, comp_id,
on_mutation)` routes tool calls; `on_mutation` fires SSE so the browser
updates as the agent works.

`_BridgeShim` (`BRIDGE`) = lightweight one-shot AIAgent for curator LLM
passes and legacy chat paths (json_task with fence/prose-tolerant parsing).

### 2.6 Evolution Curator (`curator.py`)
Runs on interval (`--curator-minutes`, default 360) + on-demand (Evolve
button, `/evolve`). Two engines layered:

**Heuristic (always, deterministic):**
- score = clicks×2 + dwell/15s + workflow_runs×3 + views×0.1
- hot components (score ≥ 8) → promote (max 2/run)
- cold visible components → shrink → (if already min) hide
- user-hidden components → propose remove
- chat prompts repeated ≥3× → mint workflow + shortcut card
- rewrites the notes/briefing card with a usage recap

**LLM refinement (when live):** gets stats, layout, feedback history,
draft mutations → better titles/rationale, may extend.

**Rejection memory (both engines):** `(op, component_id)` pairs from
proposals rejected in the last 7 days are **vetoed** — heuristics skip them,
the LLM receives them as `recently_rejected_do_not_repropose` WITH the
user's stated reasons, and its output is hard-filtered as a backstop.
Rejection without feedback still steers; feedback makes it smarter.

**Proposals, never auto-apply** (chat-driven station_mutate is the
exception by design — user watches it happen). Flow: proposal → tray →
**Try it** (non-destructive full-board preview + component diff) → Apply /
Reject(+feedback). `rebuild_from_prompt()` (`/rebuild …`) produces a
`replace_spec` proposal through the same gate, with workflow-binding repair
for LLM-invented ids.

### 2.7 Live Data Engine (`server.py` watcher)
- Per-component cadence: `props.refresh_s` > `DEFAULT_REFRESH[source]`
  (system 10s, activity 15s, git.status 20s, git.log 30s, prices 45s,
  PRs/issues 60s, datadog 30s, rss 5m, weather 10m) > 60s. Floor 5s.
- Watcher loop re-queries, SHA1-hashes the JSON payload, and on change
  emits SSE `data_changed{user_id, component_id, source}`.
- Client refetches exactly that card; per-source polling remains as
  SSE-outage fallback. Cards pulse a green dot on fresh data.
- Watcher state pruned when components are removed/hidden.
- Scaling note: this is poll-based per board; production = webhook ingest
  (GitHub etc.) emitting the same `data_changed` shape.

### 2.8 Server API (`server.py`)
```
GET  /api/state?user_id=            layout+workflows+proposals+agent+connections+curator
POST /api/telemetry                 batched interaction events
GET  /api/component/{id}/data       resolve card payload (?proposal_id= for previews)
POST /api/component/{id}/chat       scoped agent turn
POST /api/chat                      main agent turn; /rebuild + /evolve intercepts
POST /api/workflow/{id}/run         full-agent workflow execution
POST /api/layout                    save user layout VERBATIM (no repack)
POST /api/curator/run               force curator pass
GET  /api/proposal/{id}/preview     would-be spec + component-level diff
POST /api/proposal/{id}             approve/reject + feedback/sentiment
GET  /api/events                    SSE: layout_changed, proposal, tool, data_changed
POST /api/client-log                browser black box (errors, boot beacons)
GET  /healthz                       liveness + SPA presence
GET  /api/onboarding/options|complete
```
SSE event kinds: `layout_changed` (agent/approval edits → reload state),
`proposal` (curator fired), `tool{scope,name}` (agent tool activity → chat
feed), `data_changed` (targeted card refresh).

---

## 3. Frontend Systems (`web/`)

Stack: Vite + React 19 + TypeScript + Tailwind v4 (`@theme` tokens) +
Radix primitives + CVA + recharts + react-grid-layout (legacy API via
`react-grid-layout/legacy` — v2 package).

### 3.1 Design System (`index.css` + `components/ui.tsx`)
Derived from the approved reference (image6): deep navy `#0b1120` canvas,
slate `#16203a` cards, `#24304a` hairlines, **electric blue `#3b82f6` as
the only interactive accent**, emerald/amber/red reserved for data
semantics, cream `#fef3c7` reserved for future selection states. Inter with
`cv01/ss03`, weights 510/590 (`.w510/.w590`), uppercase `.microlabel`
(10.5px/590/0.09em). JetBrains Mono for SHAs/ids.

**shadcn-architecture primitives** (Radix + CVA + tailwind-merge, themed on
Station tokens): `Button` (default/secondary/ghost/destructive/outline ×
default/sm/lg/icon; primary has inset top-highlight), `Badge`
(success/warning/danger/neutral/outline), `Dialog` (blur overlay, inset-0 +
m-auto centering — see pitfall 5.3), `Tip` tooltip, `Tabs` (inset active).
**Rule: no ad-hoc buttons/badges — use the primitives.**

Signature identity: generated blue-ink engraved Hermes art
(`web/public/art/`, served at `/art/*`, `mix-blend: screen`) — sidebar
avatar, inspector header, radar center, empty states.

### 3.2 App Shell (`App.tsx`)
Fixed 228px sidebar (logo, nav with active rail, live connections, user
footer) · main column is `h-screen flex flex-col`: header (title + live
pill + actions) → stats strip (big tabular numerals + microlabels) →
**scrollable grid** → **in-flow chat console** (structural, overlap
impossible) · optional 300px Inspector rail (auto-open ≥1280px) · proposals
tray overlay · preview banner (sticky, Keep/Go back) · toast.

### 3.3 Grid & Cards (`GridBody`, `Card.tsx`)
react-grid-layout: drag by `.drag-handle` header, corner resize, vertical
compaction, indigo→blue placeholder. `onDragStop/onResizeStop` persist the
FULL spec verbatim. Card = `card-surface` + accent icon (source-aware) +
hover actions (Ask/Pop out/Refresh/Hide with tooltips) + Radix context menu
+ `body-fade` bottom mask + live pulse dot. **Pop-out**: Maximize button,
context menu, or header double-click → Dialog with View/Ask-Hermes tabs
(full-size chart + scoped chat share `useScopedChat`).

### 3.4 Chat Dock (`ChatDock.tsx`)
Three modes: bottom console (in-flow), right rail (fixed), **floating
window** (drag by titlebar, custom corner resize, z-125 above dialogs,
dock-back button). Body shared across modes: message log (you/hermes/tool
lines), busy spinner, input + primary Send.

### 3.5 Data Renderers (`DataViews.tsx`)
Per-kind views: metric w/ direction-aware delta chip (latency-up=red,
price-up=green), kv with threshold usage bars, sortable tables (click
header) with avatars (deterministic muted hues), status badges, ±% delta
coloring, favicon link lists, icon-rail feed timeline, glowing area charts
(recharts; glow = wide soft under-stroke — see pitfall 5.2; per-instance
gradient ids — see 5.1), workflow run panels, engraved-helm empty states.

`useComponentData(c)`: fetch + per-source cadence fallback + `data_changed`
subscription (`lib/api.ts` refresh bus) + flash signal.

### 3.6 Reliability & Telemetry
- Boot guard in `index.html`: not mounted in 5s → one cache-busting reload
  → branded failure screen. Inline navy background pre-CSS.
- **Client black box**: window.onerror/unhandledrejection/boot milestones
  beacon to `/api/client-log` → server log `[CLIENT] …`. Diagnose ANY user
  render failure from the server log — never debug blind again.
- Stable bundle names (`assets/index.js`) + no-store on HTML and assets:
  rebuilds can't strand an open tab (grey-screen class eliminated).
- Telemetry batcher (4s): view/click/dwell(>0.8s)/hide/move/resize/remove/
  maximize/context_menu/chats/proposal actions → curator food.

---

## 4. The Feedback Loops (why this is "amorphous")

1. **Instant loop** — user asks main chat → station_mutate → SSE
   `layout_changed` → board updates live. No approval (user is watching).
2. **Scoped loop** — per-card chat, server-enforced blast radius of one
   component.
3. **Slow loop** — telemetry accumulates → curator (heuristics + LLM +
   rejection memory) → proposal → Try-it preview → Apply/Reject(+feedback)
   → next run steers differently. Layout versions record provenance.
4. **Data loop** — watcher hashes source payloads → targeted `data_changed`
   pushes → cards update the moment reality changes.

---

## 5. Pitfalls (hard-won; do not re-learn)

1. **SVG defs are document-global.** Duplicate gradient/filter ids across
   chart instances silently break rendering (dialog charts referenced defs
   inside other offscreen SVGs). Per-instance uid'd ids, always.
2. **SVG/CSS filters don't rasterize reliably on large surfaces** in
   software rendering. Neon glow = layered soft under-stroke, not
   feGaussianBlur/drop-shadow.
3. **`translate(-50%,-50%)` dialog centering** yields fractional-pixel
   rects that break recharts' ResponsiveContainer measurement. Use
   `inset-0 + m-auto`. Charts in flex parents need a `relative` wrapper
   with `absolute inset-0` measuring box + explicit min-height.
4. **Hashed bundle names + long-lived tabs = grey screens.** Stable asset
   names + no-store, boot guard as belt-and-braces.
5. **react-grid-layout v2** renamed its API; the flat-prop component lives
   at `react-grid-layout/legacy` (`ReactGridLayout` + `WidthProvider`).
6. **Station tools must register AFTER `model_tools` import** (discovery
   nukes earlier registrations) and be exempted from tool_search deferral
   via `_HERMES_CORE_TOOLS`.
7. **AIAgent needs explicit credentials** — resolve via
   `resolve_runtime_provider()`; bare construction scans env and fails on
   OAuth-pool setups ("Model parameter is required" / auth errors).
8. **Server reflow must not fight users.** Only place components missing
   col/row. The client grid owns conflict resolution.
9. **VSCode port forwarding**: users may reach the server ONLY via
   localhost tunnels — bind 0.0.0.0, keep `/healthz`, read `[CLIENT]`
   beacons before assuming the app is broken.
10. **LLM rebuilds invent workflow ids** — repair bindings against the
    workflow table before accepting a replace_spec (drop unfixable refs).

---

## 6. Operations

```bash
# run (from hermes-agent repo root or demos/amorphous)
python demos/amorphous/serve.py --port 8877 \
  --db ~/.hermes/hermes-station.db --curator-minutes 60

# frontend
cd demos/amorphous/web && npm install && npm run build   # or npm run dev

# synthetic usage for curator demos
python demos/amorphous/simulate.py --db <db> --user <user>

# demo video (drives the real UI, records webm; ffmpeg to mp4)
node web/record-demo.mjs

# health / client diagnostics
curl :8877/healthz ; grep '\[CLIENT\]' server.log
```

Env keys (all optional): `DATADOG_API_KEY`+`DATADOG_APP_KEY`,
`BETTERSTACK_API_TOKEN`; `gh` CLI auth for GitHub sources. Model/provider
comes from the user's Hermes config (`~/.hermes/`).

## 7. Roadmap (agreed direction)

- Webhook ingest (GitHub/Datadog) → same `data_changed` shape, no polling
- Streaming agent turns into the dock (token deltas, not request/response)
- Design-token constraint in `station_mutate` (evolution can never emit
  off-system styles)
- Bespoke agent-generated components (sandboxed iframe contract) — the
  fully-amorphous tier
- Multi-user orgs: shared boards, role-scoped sections, Portal hosting
- Onboarding wow: animated board assembly narrated by the agent
