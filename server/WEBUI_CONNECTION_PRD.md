# PRD: Connecting the interfaze-agent Web UI to the Hermes Backend

Status: **Phases 1–4 implemented** (serving/auth + core data + SSE chat + long tail;
2026-07-12).
The UI bundle was copied byte-for-byte from `C:\Users\ISLAM\Desktop\agent\static\` into
[`server/webui/`](webui/) (38 files, ~869 KB); Phase 1 then modified `index.html` (doc comment),
`js/api.js`, `js/main.js`, `js/pages/login.js`, `js/pages/dashboard.js` and added `js/adapters.js`.
Phase 1 verification: `tests/server/test_webui.py` (static mount, placeholder substitution, route
precedence, DataPatch round-trip) plus a live uvicorn run exercising the login composite and
company-profile PATCH as both admin and customer.

Phase 2 added centralized request/response normalization and a real-response view-model mirror
(`webui/js/real-state.js`), then wired leads, scans, research, contacts, the five backend onboarding
steps, outreach, Company Brain, and agent-run event polling. Verification includes the
`test_phase2_core_data_flow_with_stub_executor` end-to-end contract and the full `tests/server`
suite (29 passed), plus ES-module syntax and adapter assertions.

Phase 3 added `server/chat_bridge.py`, the service-gated routes in `server/routes/chat.py`,
tenant/user-bound `chat_sessions`, restricted no-tool-by-default `AIAgent` turns, single-use SSE
capabilities, and Bearer-authenticated WebUI session/start requests. Qualification covers token
ordering, durable alternating history, concurrent-turn 409s, single-use stream IDs, disabled-route
behavior, and cross-tenant denial.

Phase 4 added real multipart document upload with the configured server-side size limit,
authenticated CSV blob downloads, WebUI-shaped pipeline/market/dashboard analytics, the promoted
onboarding/admin/WhatsApp convenience routes, and tenant-scoped list filtering. `MOCK_ROUTES` now
contains only the dormant agent-bridge probes. Qualification covers real file persistence and 413s,
analytics contracts, filters, promoted-route tenant isolation, cross-tenant export denial, and a
headless Edge hybrid browser pass across the Phase 4 user flows.

---

## 1. Goal & Background

The SPA in `server/webui/` is the customer dashboard that [ROADMAP.md](../ROADMAP.md) describes as
"developed in a separate repository". It was built against the same spec as this backend:

- [PRODUCT.md](../PRODUCT.md) **§7** defines the 216-route `/api/v1` API contract; **§8–10** define
  the frontend feature modules that `webui/js/pages/` implements.
- The backend (`server/`, FastAPI, console script `interfaze-api`, default `127.0.0.1:8000`)
  implements **216/216** spec routes, OpenAPI-verified ([STATUS.md](STATUS.md), 2026-07-12).
- The UI now runs in **hybrid mode** against the real backend. Built-in mocks remain only for the
  dormant `agent.capabilities` and `agent.status` probes.

**Goal:** serve the UI from the `interfaze-api` process and connect it to the real API in
**hybrid mode** — real backend where the spec is implemented, mocks for UI-extra routes — plus a
new **SSE chat bridge** for the dashboard's "Ask Hermes" widget.

### Target architecture (one process, one origin)

```text
interfaze-api (uvicorn, :8000)
├── GET  /                     index.html (placeholders substituted)   [new]
├── /css /js /assets ...       StaticFiles(server/webui)               [new]
├── /api/v1/*                  existing product API (Bearer JWT)       [existing]
├── GET  /api/session          ┐
├── POST /api/session/new      │ chat SSE bridge                       [new]
├── POST /api/chat/start       │ (matches webui/js/hermes-client.js)
├── GET  /api/chat/stream      ┘
└── GET  /health               existing                                [existing]
```

Same-origin serving eliminates CORS and cookie/CSRF complexity: the UI authenticates with the
Bearer token it already knows how to send (`config.authHeader` hook, `api.js:30`).

---

## 2. Serving the UI

### 2.1 Static mount

In `create_app()` ([app.py](app.py)), after all routers:

1. **`GET /` handler** — read `server/webui/index.html`, substitute the two template placeholders,
   return HTML with `Cache-Control: no-store`:
   - `__MAX_UPLOAD_BYTES__` → integer from a new `Settings.max_upload_bytes`
     (default 25 MB; `0` = "unknown/no limit" per the UI's convention).
   - `__CSRF_TOKEN_JSON__` → `json.dumps("")`. The inline fetch monkey-patch in `index.html`
     self-disables when the token is empty (`if (!token || !window.fetch) return;`), so no CSRF
     machinery is needed — auth is pure Bearer. Do **not** delete the script; neutralizing the
     token is enough and keeps the copy pristine.
   - Reference implementation of the substitution: the source repo's
     `scripts/static_webui_preview.py`.
2. **`app.mount("/", StaticFiles(directory=<server/webui>), name="webui")`** — registered **last**
   so `/api/v1/*`, `/health`, `/docs`, `/openapi.json`, and the chat-bridge routes all win
   (Starlette matches in registration order). With `webui/` as the web root, `index.html`'s
   relative hrefs (`./css/…`, `./js/…`, `./assets/…`) resolve with **no path rewriting** — the
   `/static/`-prefix remap that the old repo needed does not apply here.
3. The SPA uses a **hash router** (`#/app/...`), so no server-side SPA fallback route is required;
   the single `/` HTML route suffices. No service worker is registered.

### 2.2 Packaging

`pyproject.toml` packages `server.*` (Python modules only). Add package data so the installed
`interfaze-api` ships the UI:

```toml
[tool.setuptools.package-data]
server = ["webui/**"]
```

### 2.3 Config additions (`Settings`, [config.py](config.py))

| Key (under `interfaze_server:` in `~/.hermes/config.yaml`) | Default | Purpose |
|---|---|---|
| `max_upload_bytes` | `26214400` | injected into `index.html` |
| `webui_enabled` | `true` | escape hatch to run API-only |
| `chat_enabled` | `true` | gates the chat-bridge routes and injected WebUI flag |
| `chat_model` / `chat_toolset` | Hermes defaults / restricted | see §5.2 |

CORS (`app.py:49`) stays as-is — irrelevant in production (same origin) but useful for
`localhost:5173`-style UI dev against a running API.

---

## 3. Data Layer — Gap Analysis

### 3.1 Framing

The backend is **spec-complete**. Phase 4 promoted nine former WebUI convenience routes into
PRODUCT.md §7, leaving only the two dormant agent-probe routes outside the product contract.
Response/request shape reconciliation remains centralized in `api.js` and `adapters.js`.

### 3.2 Per-group route status

Legend: **EXISTS** = paths match · **ADAPTED** = real route with client normalization ·
**UI-STUB** = intentionally mocked dormant route.

| UI group (api.js) | Routes | Backend | Status | Group-specific notes |
|---|---|---|---|---|
| auth | 6 | [routes/auth.py](routes/auth.py) | PARTIAL | Login returns `{access_token, refresh_token, token_type, expires_in}`; UI expects `{token, user, company}` → composite adapter (§3.3.4). `/auth/me` returns flat principal vs mock's `{user, company}`. |
| admin companies | 8 | [routes/admin.py](routes/admin.py) | EXISTS | Bare-array lists (§3.3.1). |
| admin users | 8 | routes/admin.py | EXISTS | reset-password/disable return 204 → `null` through api.js; pages tolerate. |
| admin errors/logs | 2 | [routes/admin.py](routes/admin.py) | EXISTS | Admin-only, secret-safe operational summaries promoted in Phase 4. |
| company profile/positioning/sales-prefs | 6 | [routes/company.py](routes/company.py) | PARTIAL | GET returns section envelope `{company_id, data, updated_at}`; PATCH requires `{data: {...}}` (§3.3.2). |
| onboarding | 11 | [routes/onboarding.py](routes/onboarding.py) | ADAPTED | Eight persisted steps; the original five remain the completion compatibility boundary. PATCH uses `{data}` and status is normalized for the view-model. |
| documents | 6 | [routes/knowledge.py](routes/knowledge.py) | ADAPTED | Real `FormData` upload (`document_type` + `file`) with the configured server-side size limit. |
| products | 8 | routes/knowledge.py | EXISTS | extract/generate return 202 + agent run. |
| company-brain | 7 | routes/knowledge.py | PARTIAL | PATCH `{data}` envelope. |
| lead-map | 6 | [routes/sales_intelligence.py](routes/sales_intelligence.py) | PARTIAL | POST selection body: UI sends `{country_code}`, backend `CountrySelection` requires `{countries: [...]}` → adapter. |
| lead-scans | 7 | routes/sales_intelligence.py | EXISTS | |
| leads + scoring | 13 | routes/sales_intelligence.py (+ generate-outreach in [routes/outreach.py](routes/outreach.py)) | EXISTS | Server-side `country`, `status`, `scan`, `q`, and score-band filters are wired. |
| research | 7 | routes/sales_intelligence.py | EXISTS | `/research/lead/:id` ≡ `/leads/:id/research` (same handler). |
| contacts | 8 | routes/sales_intelligence.py | EXISTS | |
| outreach campaigns | 10 | routes/outreach.py | EXISTS | |
| outreach messages | 9 | routes/outreach.py | EXISTS | mark-* return 204. |
| custom-outreach | 4 | routes/outreach.py | EXISTS | |
| email integrations | 10 | [routes/integrations.py](routes/integrations.py) | PARTIAL | zoho/smtp connect endpoints exist but return **501** (unimplemented providers). |
| email send/drafts/replies | 6 | routes/outreach.py | EXISTS | |
| cc-rules | 5 | routes/outreach.py | EXISTS | |
| whatsapp integrations | 6 + 3 | routes/integrations.py | EXISTS | CRUD/test plus non-secret business-profile save/read/readiness verification. Literal `/profile` routes precede `/{integration_id}`. |
| whatsapp messages | 7 | routes/outreach.py | EXISTS | |
| linkedin | 7 | routes/outreach.py | EXISTS | |
| agent-runs | 8 | [routes/agent_runs.py](routes/agent_runs.py) | EXISTS | Poll-based (`/events`, `/logs`) — matches UI polling. |
| agent capabilities/status | 2 | — | **UI-STUB** | Only consumed by dormant `agent-bridge.js` (`config.agentAdapter.enabled = false`); intentionally mocked. |
| exports | 7 | routes/operations.py | ADAPTED | Authenticated CSV `FileResponse` downloads use fetch → blob → object URL. |
| data-sources | 8 | routes/operations.py | EXISTS | |
| activity | 5 | routes/operations.py | EXISTS | |
| analytics dashboard/pipeline/market | 3 | routes/operations.py | ADAPTED | Real dashboard composite plus backward-compatible pipeline/market fields normalized by adapters. |

**Promotion decision (Phase 4):** nine convenience routes are now PRODUCT.md contracts:
three onboarding steps, two admin operational views, three WhatsApp profile operations, and the
dashboard analytics composite. `GET /agent/capabilities` and `GET /agent/status` remain UI stubs.

### 3.3 Cross-cutting contract gaps (affect every group — fix once, centrally)

1. **List envelope.** Backend list endpoints return **bare JSON arrays**; the UI universally reads
   `{items, total}`. Fix client-side in api.js's real-fetch path:
   `Array.isArray(payload) ? {items: payload, total: payload.length} : payload`.
2. **PATCH `{data}` envelope.** All company/onboarding/brain PATCH routes take
   `DataPatch = {data: {...}}` with `extra="forbid"` ([schemas.py](schemas.py)) — flat bodies are a
   guaranteed **422**. Adapter wraps request bodies and unwraps the `{company_id, data, updated_at}`
   section envelope on GET.
3. **Error shape.** FastAPI emits `{"detail": ...}`; api.js reads `payload.error || payload.message`.
   Extend the fallback chain with `payload.detail` (and optionally add a backend exception handler
   emitting `{error, code}` — nice-to-have, not required).
4. **Auth composite.** After real login, synthesize the UI's `{token, user, company}`:
   `access_token` → `token`, then `GET /auth/me` + `GET /company/profile`. Store `refresh_token`
   for `POST /auth/refresh`. Set `config.authHeader = () => 'Bearer ' + token` at boot.
   Admin flows add the `X-Company-ID` header (already supported backend-wide via `company_scope`).
5. **Query filters.** Phase 4 handles the list filters used by leads, contacts, messages, agent
   runs, and admin logs; client filtering remains a defensive presentation layer.
6. **Non-JSON transports.** Phase 4 wires `documents.upload` through FormData and
   `exports.download` through authenticated fetch → blob → object URL.

---

## 4. Hybrid Mock/Real Mechanism (UI-side)

Dispatch is keyed by **logical route name**, keeping the hybrid boundary explicit and auditable:

1. **Mode + explicit mock set** in `api.js`:
   ```js
   config.mode = 'hybrid';            // 'mock' | 'hybrid' | 'real'
   export const MOCK_ROUTES = new Set([
     'agent.capabilities', 'agent.status',
   ]);
   ```
   Gate: `const useMock = config.mode === 'mock' || (config.mode === 'hybrid' && MOCK_ROUTES.has(name));`
   (~6-line change; exact logical names to be finalized against the route table during implementation.)
2. **Never** try-real-then-fallback-to-mock: a legitimate 404 ("lead not found") would silently
   flip to demo data and mask real bugs.
3. **New `js/adapters.js`** consulted only on the real path, keyed by name/prefix, each entry
   `{request(body), response(payload)}`. Contents = the four adapters from §3.3.1–4.
4. **Boot changes** (`main.js` / `session.js`): set `config.authHeader`; keep the mock reseed
   (`reset()`) only when `config.mode === 'mock'`. `login.js`'s demo-credentials note checks
   `config.useMocks` today → update to `config.mode === 'mock'`.

---

## 5. Chat SSE Bridge ("Ask Hermes" widget)

New backend modules: `server/routes/chat.py` (routes) + `server/chat_bridge.py` (execution),
registered **without** the `/api/v1` prefix — [`hermes-client.js`](webui/js/hermes-client.js) calls
`/api/session*` and `/api/chat/*` on the page origin.

### 5.1 Endpoint contract (must match hermes-client.js byte-for-byte)

| Endpoint | Request | Response |
|---|---|---|
| `GET /health` | — | already exists; client only checks `res.ok` |
| `GET /api/session?session_id=X&messages=0&resolve_model=0` | — | `{"session": {"session_id", "profile", "model", "workspace", "model_provider"}}`; **404** if unknown (client clears its cached id only on 404) |
| `POST /api/session/new` | `{"profile": "default"}` | `{"session": {"session_id", ...}}` |
| `POST /api/chat/start` | `{"session_id", "message", "model", "workspace", "model_provider", "profile"}` (model/workspace/provider may be empty — accept and ignore for MVP) | `{"stream_id": "..."}`; **409** if the session already has an active stream |
| `GET /api/chat/stream?stream_id=...` | EventSource | `text/event-stream`, events below |

SSE events (the client listens for exactly these four; it ignores everything else):

| `event:` | `data:` | Client behavior |
|---|---|---|
| `token` | `{"text": "<delta>"}` | append to running answer |
| `done` | `{"session": {"session_id": ...}, "usage": {"input_tokens", "output_tokens"}, "answer": "<final>"}` | uses `answer` only if no tokens arrived |
| `apperror` | `{"message": "..."}` | terminal error |
| `cancel` | `{}` | finish with accumulated text |

Plus SSE comment keepalives (`: ping`) every ~15 s; headers `Cache-Control: no-cache`,
`X-Accel-Buffering: no` (precedent: `gateway/platforms/api_server.py`).

### 5.2 Execution model

- **One `AIAgent` per turn** ([run_agent.py:416](../run_agent.py)): `quiet_mode=True`,
  `skip_context_files=True`, `skip_memory=True`, **restricted toolset** (read-only/research or none
  for MVP — this is an untrusted-tenant surface), modest `max_iterations` (~15), and an
  `ephemeral_system_prompt` composed from `AgentRunService.company_context(company_id)`
  ([agent_service.py](agent_service.py)) so answers carry tenant sales context.
- Wire **only `stream_delta_callback`** → queue `token` events (ignore the terminal `None` flush;
  don't forward the whole-response fallback — `done.answer` covers the no-stream case). Blocking
  `agent.run_conversation(message, conversation_history=history, stream_callback=…)`
  ([run_agent.py:5745](../run_agent.py)) returns `{"final_response", "usage", ...}` → `done`.
- **Threading:** dedicated module-level `ThreadPoolExecutor` (pattern: `AgentRunService.pool`),
  shut down in the app lifespan alongside the existing pool. Registry
  `stream_id → {queue, session_id, company_id, principal_id, created_at, done}`; worker pushes
  `(event, payload)` tuples into a thread-safe `queue.Queue`; the endpoint returns a
  `StreamingResponse` whose sync generator pulls with a timeout (keepalive on timeout) until the
  terminal event. TTL-GC finished streams (~60 s) and idle sessions (~24 h).
- **Cancellation:** the client cancels by closing the EventSource (no cancel POST exists). MVP: on
  generator exit, mark the stream abandoned and discard the result; the turn runs to completion in
  the background, bounded by `max_iterations`.

### 5.3 Auth & session persistence

- `hermesApi()` now sends the stored **Authorization** header, while `EventSource` **cannot** send
  headers. Implemented design:
  - Require Bearer (`current_principal` / `company_scope`) on `GET /api/session`,
    `POST /api/session/new`, `POST /api/chat/start` → **one small UI change** in
    `hermes-client.js`: attach `Authorization: Bearer <token>` from `session.js`.
  - `GET /api/chat/stream` is authorized by the `stream_id` itself: a single-use, short-lived
    `secrets.token_urlsafe` **capability token** bound to the session's company and invalidated
    after first attach. Without Bearer on `/api/chat/start`, the bridge would be effectively
    unauthenticated — the UI change is mandatory, not optional.
- **Session store:** a new tenant-scoped `chat_sessions` table via the existing `Database` layer
  ([db.py](db.py)): `id, company_id, user_id, profile, history JSON, created_at, updated_at`.
  History passed as `conversation_history` per turn, appended after `done`, capped (last ~20
  messages). **Do not** reuse `hermes_state.SessionDB` — it is the operator's global agent-session
  store; mixing tenant chat into it risks cross-tenant context leakage.

---

## 6. Backend Change Checklist

| # | Change | Where | Phase |
|---|---|---|---|
| 1 | `GET /` index handler + placeholder substitution | app.py | 1 |
| 2 | `StaticFiles` mount (last) | app.py | 1 |
| 3 | `Settings.max_upload_bytes`, `webui_enabled`, `chat_enabled`, chat model/toolset | config.py | 1/3 |
| 4 | package-data for `server/webui/**` | pyproject.toml | 1 |
| 5 | ✅ Chat bridge (routes + executor + `chat_sessions` table + lifespan shutdown) | routes/chat.py, chat_bridge.py, db.py | 3 |
| 6 | *(optional)* `{error, code}` exception handler | app.py | 2 |
| 7 | ✅ Promoted WebUI convenience routes; `/analytics/dashboard` composite; whatsapp `/profile` **before** `/{integration_id}` | routes/* | 4 |
| 8 | ✅ List query-param filtering (leads plus sibling UI list paths, admin logs) | routes/* | 4 |

## 7. UI Change Checklist (all inside `server/webui/`)

| # | Change | File |
|---|---|---|
| 1 | ✅ `config.mode` + agent-stub-only `MOCK_ROUTES`; list-wrap; errors; multipart/blob transports | js/api.js |
| 2 | ✅ Adapter layer (envelopes, auth composite, lead-map body, analytics/integration shapes) | js/adapters.js |
| 3 | ✅ `Authorization: Bearer` in `hermesApi()` | js/hermes-client.js |
| 4 | ✅ Set `config.authHeader` at boot; preserve the shared hybrid view-model seed | js/main.js, js/session.js |
| 5 | ✅ Demo-note check `useMocks` → `mode === 'mock'` | js/pages/login.js |
| 6 | ✅ Gate Ask Hermes on the server-injected `chat_enabled` value | js/pages/dashboard.js |

## 8. Security

- **Tenant isolation:** every wired route already flows through `current_principal` /
  `company_scope`; the chat bridge must use the same dependencies, and its system prompt must only
  include the authenticated principal's `company_context`.
- **Chat is an untrusted-tenant surface:** restricted toolset (no shell/file tools in MVP), bounded
  iterations, per-session single active stream, pool-size cap.
- **Stream capability tokens:** unguessable, single-use, short-lived, company-bound (EventSource
  header limitation).
- **Upload limit** enforced server-side (`max_upload_bytes`), not just injected into the page.
- **CORS** list stays dev-only; production is same-origin.
- Mock data ships in the bundle (harmless demo content) — acceptable; it never touches the backend.

## 9. Phased Rollout

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **1 — Serve + Auth** | Static mount, placeholders, hybrid gate, adapters, real login/me/company | Login against real API; dashboard renders; company profile round-trips (PATCH 200, not 422) |
| **2 — Core data** ✅ | Leads, scans, research, contacts, onboarding (5 spec steps), outreach, agent-runs polling | Silverline demo flow (PRODUCT.md §11) runs against the real API with `StubRunExecutor` |
| **3 — Chat bridge** ✅ | §5 endpoints + `chat_enabled=true` + widget activation | Ask Hermes streams tokens end-to-end; 409 on concurrent stream; cross-tenant test fails closed |
| **4 — Long tail** ✅ | Uploads (FormData), exports (blob download), analytics shape reconciliation, promoted UI-extras, server-side filters | `MOCK_ROUTES` contains only agent-bridge stubs; full server suite passes |

## 10. Testing

- **Backend:** extend the [tests/server/test_api_mvp.py](../tests/server/test_api_mvp.py) pattern
  (`create_app(run_executor=StubRunExecutor())`): static-mount smoke (GET `/` → 200, placeholders
  substituted, no literal `__CSRF_TOKEN_JSON__`; GET `/js/main.js` → 200; GET `/api/v1/...` still
  wins over the mount), chat-bridge tests with a fake agent (token order, `done` shape, 404/409,
  stream-id single-use, tenant isolation).
- **UI:** with `config.mode='hybrid'`, a scripted browser pass over Phase-appropriate pages;
  explicit check that a real 404 surfaces as an error (no silent mock fallback).
- **Contract:** regenerate OpenAPI and re-run the PRODUCT.md 216-route comparison after any
  backend additions.

## 11. Risks

1. **`DataPatch extra="forbid"` 422s are invisible under mocks** — the most likely "worked in demo,
   broke on real" failure. Covered by adapter + Phase-1 exit criterion.
2. **`/health` already returns 200**, so availability must include the injected `chat_enabled`
   flag and the health response's `chat_enabled` field. Phase 3 implements both checks.
3. **EventSource cannot authenticate** — the stream_id-capability design is mandatory; without the
   Bearer change to `/api/chat/start` the bridge is open.
4. **Blocking agent turns hold worker threads for minutes** — pool sizing, per-session cap, and
   lifespan cancellation required.
5. **Server-side filter drift** — Phase 4 wires the filters used by the WebUI; new list-page filters
   must be added to the corresponding backend route instead of relying only on client filtering.
6. **Analytics shape drift** — Phase 4 returns and adapts the WebUI contract while retaining the
   original backend fields; tests assert both pipeline/market safety and the dashboard composite.
7. **Route shadowing** — the implemented WhatsApp `/profile` routes must remain before
   `/{integration_id}`.
8. **Real `HermesProcessExecutor` needs credentials** — local/UI development runs with
   `StubRunExecutor`; document the dev flag in the runbook.

---

## Appendix A — Copied bundle manifest (`server/webui/`, 38 files, byte-identical to source)

```text
index.html  favicon.ico  favicon-32.png  favicon-192.png  favicon-512.png  apple-touch-icon.png
assets/   profile.jpeg  world.svg
css/      tokens.css  app.css
js/       main.js  router.js  shell.js  ui.js  api.js  adapters.js  real-state.js  session.js  icons.js
          hermes-client.js  agent-bridge.js
js/mocks/ handlers.js  seed.js  db.js          (kept — required by hybrid mode)
js/pages/ _page-utils.js  access-pending.js  admin.js  agent-runs.js  analytics.js
          company-brain.js  contacts.js  custom-outreach.js  dashboard.js  integrations.js
          lead-map.js  leads.js  login.js  onboarding.js  outreach.js  settings.js
```

Not copied from the source repo (superseded by this backend): `server.py`, `api/`, `tests/`,
`scripts/`, `docs/`.

## Appendix B — Key reference files

| Contract | File |
|---|---|
| UI route table + client config hooks | `webui/js/api.js` |
| Chat client (SSE contract to preserve) | `webui/js/hermes-client.js` |
| Mock response shapes to mirror | `webui/js/mocks/handlers.js` |
| Legacy SSE server (bridge reference) | source repo `api/streaming.py` |
| Placeholder substitution reference | source repo `scripts/static_webui_preview.py` |
| API spec (authoritative) | [PRODUCT.md §7](../PRODUCT.md) |
| Backend REST contract notes | [UI_INTEGRATION.md](UI_INTEGRATION.md) |
| Agent execution surface | `run_agent.py:416` (AIAgent), `:5745` (run_conversation) |
| Threading/eventing pattern | [agent_service.py](agent_service.py) |
-----------------------------------------------------

To finish the job, complete these in order:

Supabase qualification
Apply 001_initial.sql and 002_chat_sessions.sql.
Configure Supabase Auth, Postgres, private Storage, and RLS.
Create two tenants and prove cross-tenant reads/writes fail.
Test document uploads, CSV exports, and chat persistence.
Gmail OAuth sandbox
Configure OAuth credentials and INTERFAZE_CREDENTIAL_KEY.
Connect Gmail.
Create draft, send approved email, refresh token, read reply/status.
Verify idempotency and tenant isolation.
Microsoft Graph sandbox
Repeat the Gmail flow with Microsoft 365.
WhatsApp Cloud sandbox
Configure Meta test number, WABA, token, and public webhook.
Verify profile, send approved message, process delivery/reply/failure/opt-out events.
Test retry and ambiguous-timeout behavior.
Production-model acceptance Run the complete real-Hermes workflow:
upload → process → Company Brain → approve → scan
→ research → contacts → outreach → approve → draft/send
Record model, tokens, cost, duration, output quality, and failures.

Packaging and dependency lock
Run uv lock.
Build/install the package in a clean environment.
Confirm the WebUI and Supabase migrations are included.
Start interfaze-api from the installed package.
Final regression
Run the complete test suite.
Re-run the headless browser acceptance against the production-like setup.
Confirm OpenAPI exposes all 216/216 contracts.
Run JavaScript syntax and Python compilation checks.
Cleanup and documentation
Stop the local server.
Remove generated .browser-phase3.db*, .pytest-tmp-*, and .local-hermes/ artifacts when no longer needed.
Update STATUS.md with external test evidence.
Record remaining known limitations, if any.
After these pass, the implementation is release-ready.