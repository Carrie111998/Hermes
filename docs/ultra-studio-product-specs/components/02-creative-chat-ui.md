# Creative Chat UI

Status: partial — a real Hermes gateway chat client ships today at `/chat`; all creative-specific layers (media cards, model picker, thinking stream, media job events, typed errors, durable approvals, entity pickers, creative inspector) are spec-only.
Date: 2026-06-10

Sources:

- Docs: `docs/ultra-studio-product-specs/00-index.md`, `01-product-surface.md`,
  `02-agent-runtime-contract.md`, `03-media-asset-contract.md`,
  `04-skill-tool-prompt-contract.md`, `06-delivery-plan.md`,
  `docs/hermes-real-chat-agent-ui.md`
- Code: `web/src/pages/ChatPage.tsx`, `web/src/hooks/useGatewayChat.ts`,
  `web/src/lib/gatewayClient.ts`, `web/src/lib/slashExec.ts`,
  `web/src/lib/chatUpload.ts`, `web/src/components/chat/*`,
  `web/src/components/ToolCall.tsx`, `web/src/components/ChatSidebar.tsx`,
  `tui_gateway/server.py`, `hermes_cli/dashboard_uploads.py`,
  `hermes_cli/ultra_studio_skills.py`, `tools/clarify_tool.py`,
  `skills/creative/*`, `plugins/image_gen/atlas/`, `plugins/video_gen/atlas/`

## Purpose & Scope

The Creative Chat UI is the "Center: Creative Session" surface of Ultra Studio.
It is the conversation column where the user states intent, uploads media,
answers agent questions, approves actions, and watches generation progress.
It pairs with the right-hand inspector (live panel) and must never auto-start
generation on open.

In scope:

- Chat transcript: streamed assistant text, user input, slash commands.
- Media upload and attachment flow into the agent session.
- Tool status, clarify/approval/sudo/secret prompt cards, interrupt.
- The inspector panel as it relates to the chat session (session info, tools,
  pending prompts today; job/asset/QA/lineage details per spec).
- Session create/resume lifecycle from the dashboard.

Out of scope (covered by sibling specs): the media/asset contract itself (03),
the agent runtime event contract (02), skill/tool/prompt contracts (04), and
the surrounding product shell/left nav (01).

## Implementation Status

| Layer | State | Evidence |
|---|---|---|
| Gateway chat client (WS JSON-RPC, session create/resume, streamed text) | Implemented | `web/src/pages/ChatPage.tsx`, `web/src/hooks/useGatewayChat.ts`, `web/src/lib/gatewayClient.ts` |
| Media upload + attachment chips + attach-by-path | Implemented | `web/src/lib/chatUpload.ts`, `web/src/components/chat/ChatComposer.tsx`, `hermes_cli/dashboard_uploads.py` |
| Slash commands with directive fallback | Implemented | `web/src/lib/slashExec.ts` |
| Tool-call status feed (running/done/error, inline diff, live preview) | Implemented | `web/src/components/ToolCall.tsx`, `web/src/components/chat/ChatInspector.tsx` |
| Clarify / approval / sudo / secret prompt cards | Implemented | `web/src/components/chat/PendingPromptPanel.tsx`, `tools/clarify_tool.py` |
| Interrupt, reconnect, connection badge | Implemented | `web/src/pages/ChatPage.tsx`, `web/src/hooks/useGatewayChat.ts` |
| Ultra Studio skill allowlist + 4 creative SKILL.md files | Implemented | `hermes_cli/ultra_studio_skills.py`, `skills/creative/{workflow-router,infographic-md-flow,media-qa,prompt-repair}/` |
| Atlas image/video provider plugins | Implemented | `plugins/image_gen/atlas/`, `plugins/video_gen/atlas/` |
| Media cards in transcript (preview, status, actions) | Specified, not built | `01-product-surface.md` §Center, `03-media-asset-contract.md` §Asset Card UI; zero media rendering in `ChatMessageList.tsx` |
| Model picker in the creative session | Specified, not built | `01-product-surface.md` §Center; new ChatPage has none (only legacy `ChatSidebar.tsx` has `ModelPickerDialog`) |
| Thinking/reasoning phase display | Specified, not built | `01-product-surface.md` §Required States; gateway declares `thinking.delta`/`reasoning.delta` but `useGatewayChat.ts` ignores them |
| `media_job.created/updated`, `asset.ready` events | Specified, not built | `02-agent-runtime-contract.md` §Event Stream; absent from `tui_gateway/` and `gateway/` (rg verified) |
| `ultra_media_job_*` / `ultra_media_constraints_get` tools | Specified, not built | `03-media-asset-contract.md` §Required Job Tools; not present in `tools/` |
| Typed error contract + recovery cards | Specified, not built | `02-agent-runtime-contract.md` §Error Contract; UI shows generic string errors only |
| Inspector job/asset/QA/lineage/download/convert features | Specified, not built | `01-product-surface.md` §Inspector; `ChatInspector.tsx` shows session + tools only |
| Entity pickers (soul_id/element/voice/language) | Specified, not built | `docs/hermes-real-chat-agent-ui.md` §References And Asset UI |
| Attachments as typed `media_input` refs | Specified, not built | `03-media-asset-contract.md` §Goal; today attachments flatten to text markers |
| Durable approvals + active jobs on resume | Specified, not built | `02-agent-runtime-contract.md` §Human Approval Gateway, §Acceptance; pending prompt is in-memory only |
| Approval decision-set mismatch (spec `approve/edit/reject/respond` vs code `once/session/deny`) | Open gap | `02-agent-runtime-contract.md` vs `PendingPromptPanel.tsx` |
| Event-name mismatch (`approval.requested` vs `approval.request`; `tool.error` vs `tool.complete` with error) | Open gap | `02-agent-runtime-contract.md` vs `tui_gateway/server.py` |
| Markdown rendering, SlashPopover wiring, message `error` status, tool feed 24-cap, hardcoded greeting | Open gap | `web/src/components/chat/ChatMessageList.tsx`, `web/src/components/SlashPopover.tsx`, `web/src/components/chat/contracts.ts`, `useGatewayChat.ts` |

Never read the "Specified, not built" rows as shipped behavior.

## User Entry Points

| Entry | State | Detail |
|---|---|---|
| Dashboard route `/chat` | Implemented | Persistent host in `web/src/App.tsx` (`ChatRouteSink` claims `/chat`); rendered only when the server injects `window.__HERMES_DASHBOARD_EMBEDDED_CHAT__=true` via `hermes dashboard --tui` or `HERMES_DASHBOARD_TUI=1` (`web/src/lib/dashboard-flags.ts`) |
| Sessions page "resume in chat" | Implemented | Navigates to `/chat?resume=<session_id>` (`web/src/pages/SessionsPage.tsx`) |
| Plugin extension points | Implemented | `chat:top` / `chat:bottom` PluginSlots (`web/src/pages/ChatPage.tsx`); plugins may replace the page via `tab.override:/chat` (`web/src/App.tsx`) |
| Left-nav "New task" entry | Planned | `01-product-surface.md` §Left Nav Shell; no such nav exists in `web/src/App.tsx` |
| Tasks page "Continue work" reopening creative work with artifacts | Planned | `01-product-surface.md` §Main Jobs |

## Feature List

| # | Feature | Tag | Source |
|---|---|---|---|
| 1 | WebSocket gateway chat client with `session.create` / `session.resume` | Implemented | `web/src/hooks/useGatewayChat.ts`, `web/src/lib/gatewayClient.ts` |
| 2 | Streaming assistant text via `message.start/delta/complete` | Implemented | `web/src/hooks/useGatewayChat.ts` |
| 3 | User text input: Enter sends, Shift+Enter newline, disabled while running/disconnected | Implemented | `web/src/components/chat/ChatComposer.tsx` |
| 4 | Slash commands via `slash.exec` with `command.dispatch` fallback (exec/plugin/alias/skill/send) | Implemented | `web/src/lib/slashExec.ts` |
| 5 | Media file upload (paperclip) → `POST /api/chat/uploads` → `input.detect_drop` / `image.attach` | Implemented | `web/src/lib/chatUpload.ts`, `hermes_cli/dashboard_uploads.py` |
| 6 | Attach by local filesystem path | Implemented | `web/src/components/chat/ChatComposer.tsx` |
| 7 | Attachment chips with name/meta (dimensions, token estimate) and remove | Implemented | `ChatComposer.tsx`, `ChatMessageList.tsx` |
| 8 | Tool-call status feed: running/done/error with context, preview, summary, inline_diff, auto-expanded errors | Implemented | `ChatInspector.tsx`, `ToolCall.tsx` |
| 9 | Clarify question card: choice buttons + free-text → `clarify.respond` | Implemented | `PendingPromptPanel.tsx`, `tools/clarify_tool.py` |
| 10 | Approval card: approve once / approve session / deny → `approval.respond` | Implemented | `PendingPromptPanel.tsx` |
| 11 | Sudo password and secret value prompts → `sudo.respond` / `secret.respond` | Implemented | `PendingPromptPanel.tsx` |
| 12 | Interrupt running turn via `session.interrupt` | Implemented | `ChatPage.tsx`, `useGatewayChat.ts` |
| 13 | Reconnect button (rebuilds GatewayClient, clears tools/pending state) | Implemented | `useGatewayChat.ts` |
| 14 | Connection state badge: idle/connecting/live/closed/error | Implemented | `ChatInspector.tsx` |
| 15 | Resume session from Sessions page via `/chat?resume=<id>` | Implemented | `web/src/pages/SessionsPage.tsx` |
| 16 | Embedded-chat gating via `__HERMES_DASHBOARD_EMBEDDED_CHAT__` | Implemented | `web/src/lib/dashboard-flags.ts`, `docs/hermes-real-chat-agent-ui.md` |
| 17 | Persistent ChatPage host across route changes; plugin `tab.override` for `/chat` | Implemented | `web/src/App.tsx` |
| 18 | Plugin slots `chat:top` / `chat:bottom` | Implemented | `web/src/pages/ChatPage.tsx` |
| 19 | Empty-state greeting screen (hardcoded text) | Implemented | `ChatMessageList.tsx` |
| 20 | No auto-generation on open — `session.create` only, user intent drives execution | Implemented | `useGatewayChat.ts`; matches `01-product-surface.md` |
| 21 | Ultra Studio skill allowlist helper + 4 creative SKILL.md files | Implemented | `hermes_cli/ultra_studio_skills.py`, `skills/creative/*` |
| 22 | Atlas image/video provider plugins | Implemented | `plugins/image_gen/atlas/`, `plugins/video_gen/atlas/` |
| 23 | Media cards in transcript: preview/status/download/inspect/reuse/convert-to-element/create-character | Planned | `01` §Center, `03` §Asset Card UI |
| 24 | Model picker inside the creative session | Planned | `01` §Center |
| 25 | Thinking/reasoning phase display | Planned | `01` §Required States "Thinking" |
| 26 | Durable media job events `media_job.created/updated`, `asset.ready` | Planned | `02` §Event Stream |
| 27 | `ultra_media_job_create/status/cancel/retry/finalize` + `ultra_media_constraints_get` tools | Planned | `03` §Required Job Tools, `04` |
| 28 | Typed error contract with actionable recovery cards | Planned | `02` §Error Contract |
| 29 | Inspector job/asset details: prompt, seed, dimensions, lineage, QA evidence, download, retry/repair plan | Planned | `01` §Inspector |
| 30 | Entity picker UI for soul_id / element / voice / language in ask-user-question | Planned | `hermes-real-chat-agent-ui.md` §References And Asset UI |
| 31 | Attachments as typed `media_input` asset refs instead of text-prefix markers | Planned | `03` §Goal, `00` §Acceptance |
| 32 | Session state restoring active jobs, selected assets, task files on resume | Planned | `02` §Session Lifecycle |
| 33 | Approval persistence across page refresh from durable state | Planned | `02` §Human Approval Gateway |
| 34 | workflow-router runtime connection / one-useful-question routing | Planned | `04` §Clarification Rules, `06` P0 item 4; SKILL.md exists, runtime wiring unverified |
| 35 | Approval decision-type mismatch: spec `approve/edit/reject/respond` vs implemented `once/session/deny`; "edit" and "respond" have no UI or RPC | Gap | `02` vs `PendingPromptPanel.tsx` |
| 36 | Event-name mismatch: spec `approval.requested/resolved` vs code `approval.request/respond`; spec `tool.error` vs code `tool.complete`-with-error | Gap | `02` vs `tui_gateway/server.py` |
| 37 | Message error status: `WebChatStatus` declares `'error'` but no code path sets it | Gap | `contracts.ts` vs `useGatewayChat.ts` |
| 38 | Markdown rendering: `Markdown.tsx` exists but `ChatMessageList` renders raw text | Gap | `ChatMessageList.tsx` |
| 39 | SlashPopover autocomplete exists but is not wired into the ChatPage composer | Gap | `web/src/components/SlashPopover.tsx` (imported by no other file) |
| 40 | Hardcoded personalized greeting "lif, what are we creating today?" — no user-name source or i18n | Gap | `ChatMessageList.tsx` |
| 41 | `tool.generating` and `background.complete` events declared but unhandled in web chat | Gap | `gatewayClient.ts` vs `useGatewayChat.ts` |
| 42 | Tool feed silently truncates at 24 entries with no indicator (`TOOL_LIMIT`) | Gap | `useGatewayChat.ts` |

## State Machine

### Connection state (implemented — `web/src/lib/gatewayClient.ts`)

```text
idle -> connecting -> open -> closed | error
```

- `connecting`: triggered by mount or by the user pressing reconnect.
- `open`: WS handshake (plus ticket auth when gated) succeeds.
- `closed` / `error`: socket close or failure. Reconnect rebuilds the client
  and clears tool entries and pending prompts.

### Message status (implemented — `web/src/components/chat/contracts.ts`)

```text
streaming -> complete
```

`'error'` is declared in the type but never set by any code path (gap, item 37).
Transitions are driven by gateway `message.start/delta/complete` events.

### Tool entry status (implemented — `web/src/components/ToolCall.tsx`)

```text
running -> done | error
```

Driven by `tool.start` → `tool.progress` → `tool.complete`; the error state
comes from a `tool.complete` payload with an `error` field, not a separate
`tool.error` event (gap, item 36).

### Pending prompt (implemented — `contracts.ts`, `useGatewayChat.ts`)

```text
none -> clarify | approval | sudo | secret -> resolved
```

- Entered by gateway `clarify.request` / `approval.request` / `sudo.request` /
  `secret.request`.
- Resolved by the user via the matching `*.respond` RPC, or cleared by
  interrupt/reconnect.
- Not durable: a page refresh or reconnect loses the pending prompt (gap vs
  `02` §Human Approval Gateway).

### Creative session states (spec-only — `01-product-surface.md` §Required States)

```text
Empty -> Thinking -> Waiting for user -> Creating -> Complete | Failed
```

Each state pairs center-column behavior with inspector behavior. Triggers:
user submits a prompt (Empty→Thinking), agent asks a question or requests
approval (→Waiting for user), media job starts (→Creating), job/asset
completes (→Complete), typed error (→Failed). None of this state labeling
exists in code today.

### Asset lifecycle and media-job chain (spec-only — `03-media-asset-contract.md`)

```text
uploading -> processing -> ready -> archived        (failure: failed | revoked | deleted)
job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
```

Triggered by the planned `ultra_media_job_*` tool layer and gateway
`media_job.*` / `asset.ready` events; none exist in code.

## APIs & Events

### Transport (exists)

| Surface | Detail |
|---|---|
| `WS /api/ws` | JSON-RPC gateway, newline-delimited dialect shared with the Ink TUI; default request timeout 120s (`web/src/lib/gatewayClient.ts`) |
| `GET /api/auth/ws-ticket` | Single-use ticket auth when `window.__HERMES_AUTH_REQUIRED__`; gated mode rejects legacy `?token=` |
| `POST /api/chat/uploads` | Raw body + `X-Hermes-Filename` header; returns `{path, name, mime_type, size}`; 415 for non-media; 200MB cap (`hermes_cli/dashboard_uploads.py`) |
| `GET /api/events?channel=<id>` | Legacy passive fanout used only by the PTY-pane `ChatSidebar.tsx` |

### RPC methods (exist — verified in `tui_gateway/server.py`)

`session.create`, `session.resume`, `session.list`, `session.history`,
`session.interrupt`, `session.steer`, `session.branch`, `session.compress`,
`session.undo`, `session.delete`, `session.title`, `session.usage`,
`session.status`, `session.save`, `session.close`, `session.activate`,
`session.active_list`, `session.most_recent`, `prompt.submit`,
`prompt.background`, `input.detect_drop`, `input.request`, `image.attach`,
`slash.exec`, `command.dispatch`, `command.resolve`, `clarify.respond`,
`approval.respond`, `sudo.respond`, `secret.respond`.

### Events (exist — emitted/declared in `tui_gateway/*.py` and `gatewayClient.ts`)

| Event | Handled by chat UI? |
|---|---|
| `gateway.ready`, `session.info` | Yes |
| `message.start`, `message.delta`, `message.complete` | Yes |
| `status.update` | Yes |
| `tool.start`, `tool.progress`, `tool.complete` | Yes (also `tool.started` declared) |
| `clarify.request`, `approval.request`, `sudo.request`, `secret.request` | Yes |
| `error` | Yes (generic banner + system message) |
| `thinking.delta`, `reasoning.delta`, `reasoning.available` | No — declared in `gatewayClient.ts`, ignored by `useGatewayChat.ts` |
| `tool.generating`, `background.complete`, `skin.changed` | No — declared, unhandled |

### Proposed (spec-only — do not assume these exist)

| Contract | Source | Status |
|---|---|---|
| Events `media_job.created`, `media_job.updated`, `asset.ready`, `approval.requested`, `approval.resolved`, `tool.error` | `02-agent-runtime-contract.md` §Event Stream | None exist in `tui_gateway/` or `gateway/` (rg verified zero matches) |
| Tools `ultra_media_job_create/status/cancel/retry/finalize`, `ultra_media_constraints_get` | `03-media-asset-contract.md` §Required Job Tools | Not present in `tools/` |
| Tools `ultra_asset_upload/list/inspect/download/promote`, `ultra_model_catalog`, `ultra_prompt_compile/enhance` | `03`/`04` spec pack | Not present in `tools/` |

A canonical event vocabulary table reconciling spec names with gateway names
is required before frontend media work starts (see Open Questions).

## Data Model

### Implemented (frontend, in-memory React state — `web/src/components/chat/contracts.ts` unless noted)

| Entity | Fields | Persistence |
|---|---|---|
| `WebChatMessage` | `id: string`, `role: 'user'\|'assistant'\|'system'\|'tool'`, `text: string`, `status?: 'streaming'\|'complete'\|'error'`, `attachments?: WebChatAttachment[]` | React state only; history rehydrated lossily via `session.resume` |
| `WebChatAttachment` | `id`, `kind: 'image'\|'file'`, `name`, `path`, `text` (the prompt marker string), `meta?` | React state only |
| `WebChatSessionInfo` | `cwd?`, `model?`, `provider?`, `profile_name?`, `credential_warning?` | From `session.info` event |
| `WebChatPendingPrompt` | union: clarify `{requestId, question, choices?}` \| approval `{command, description}` \| sudo `{requestId, password}` \| secret `{requestId, envVar, prompt, value}` | React state only; lost on refresh/reconnect |
| `ToolEntry` (`web/src/components/ToolCall.tsx`) | `kind:'tool'`, `id`, `tool_id`, `name`, `context?`, `preview?`, `summary?`, `error?`, `inline_diff?`, `status:'running'\|'done'\|'error'`, `startedAt`, `completedAt?` | React state; capped at 24 entries |
| `ChatUploadResponse` (`web/src/lib/chatUpload.ts`) | `path`, `name`, `mime_type`, `size` | File stored under `HERMES_HOME/dashboard-uploads/<YYYYMMDD>/` with timestamp+token-prefixed safe filename |
| `WebChatImageAttachResult` / `WebChatDetectDropResult` | `attached?`, `path?`, `name?`, `text?`, `count?`, `width?`, `height?`, `token_estimate?`, `message?`; detect-drop adds `matched?`, `is_image?` | Transient RPC results |
| Resume message shape (`WebChatSessionResumeResult`) | `{role?, text?, name?, context?}[]` | Server-side session history; restores text only — tools, attachments, jobs, pending prompts are not restored |

### Specified, not built

| Entity | Fields | Source |
|---|---|---|
| `MediaJob` envelope | `job_id`, `session_id`, `run_id`, `tool_call_id`, `provider`, `model`, `media_type`, `mode`, `status`, `input_assets`, `prompt`, `negative_prompt`, `provider_constraints`, `seed`, `tokenrouter_decision_id`, `output_assets`, `error` | `03-media-asset-contract.md` §Media Job Envelope |
| Asset types | `media_input`, `image_job`, `video_job`, `audio_job`, `element`, `character`, `soul_id`, `task_file` | `03` §Asset Types |
| Router output | `{intent, execution_mode, workflow_skill, primary_tool, asset_roles, missing, handoff}` | `04-skill-tool-prompt-contract.md` |
| Session state on resume | user/workspace/project ids, model selection, active skill profile, sandbox id, task files root, active media jobs, selected assets | `02` §Session Lifecycle |

## UI Behavior

### Layout (implemented)

- Dark themed full-height panel. Center column: header / error banner /
  message list / composer. Fixed 320px (`w-80`) right inspector, hidden below
  the `lg` breakpoint (`ChatPage.tsx`, `ChatInspector.tsx`).
- Header: bot icon, "Hermes Agent" title, provider·model subtitle (model
  basename via `displayModel`), connection badge, stop button only while
  running, reconnect icon button.

### Transcript (implemented)

- User messages right-aligned teal-tinted bubbles; assistant left dark
  bubbles; system amber bubbles; role icons; streaming spinner inside the
  active assistant bubble.
- Text renders as plain `whitespace-pre-wrap` only: no Markdown, no
  image/video previews, no media cards, no inline ask-user-question cards
  (prompt cards live in the inspector).
- Empty state: large bot icon + hardcoded headline
  "lif, what are we creating today?" (gap: no user-name source, no suggested
  tasks despite `01` §Required States "Prompt input and suggested tasks").
- Auto-scrolls to bottom (smooth) on every messages change; no scroll-lock
  when the user scrolls up. No virtualization or pagination.

### Composer (implemented)

- Textarea: Enter sends, Shift+Enter newline. Send disabled unless connection
  open + session exists + not running + non-empty input.
- Paperclip upload: single file per pick, `accept=image/*,video/*,audio/*`.
- Folder icon toggles an attach-by-local-path row (`sm`+ screens only).
- Attachment chip row above the input with remove buttons and meta
  (dimensions/token estimate); `attachError` text renders under the composer.
- Attachment submission: `attachPath()` calls `input.detect_drop` first,
  falls back to `image.attach`; the resulting text marker (e.g.
  `[User attached image: name]`) is joined as a plain-text prefix to the
  outbound `prompt.submit` text. Attachments are not typed asset refs.

### Slash input (implemented)

- Any input starting with `/` goes through `executeSlash`; outputs render as
  system messages; skill directives show a "⚡ loading skill: <name>" system
  line. The `SlashPopover` autocomplete component exists but is not wired in.

### Inspector (implemented subset)

- Session card: model, session id, status line preferring
  `error ?? credential_warning ?? currentStatus`.
- "Input needed" `PendingPromptPanel` when a prompt is pending: clarify
  (choice buttons + free text), approval (approve once / approve session /
  deny), sudo password, secret value.
- Scrollable Tools card with per-tool collapsible detail; "no tool calls yet"
  empty text. Error rows auto-expand.
- Contains none of the spec inspector features: job status/progress, asset
  preview, prompt/seed/dimensions/duration/lineage, QA result + observed
  evidence, download/export, Convert to Element, Create Character,
  retry/repair plan (`01` §Inspector).

### Status transitions (implemented)

- Pending prompts set `running=true`; deny → status "denied"; interrupt →
  "interrupted"; a clarify answer echoes back as a user message.

### Resume (implemented, lossy)

- History renders as completed messages only; tools, attachments, active
  jobs, selected assets, and pending prompts are not restored.

### Planned UI behavior (spec-only)

- Media cards inline in the transcript with preview, status, type,
  provider/model, dimensions/duration, prompt hash, input refs, job id,
  download, inspect, reuse, convert-to-element, create-character; cards must
  not expose internal filesystem paths by default (`03` §Asset Card UI).
- Ask-user-question cards inline in the transcript with entity pickers for
  `soul_id` / element / voice / language.
- Error cards with per-code recovery actions.
- Thinking-phase display driven by `thinking.delta`.
- Model picker in the session header.
- Suggested tasks on the empty state.
- Six required states (Empty/Thinking/Waiting for user/Creating/Complete/
  Failed) each with paired center + inspector behavior (`01` §Required
  States).

## Permissions & Error Handling

### Implemented

| Control | Behavior |
|---|---|
| Chat surface gating | Embedded chat renders only when the server injects `__HERMES_DASHBOARD_EMBEDDED_CHAT__` (`hermes dashboard --tui`); otherwise `/chat` is absent |
| WS auth | Gated mode rejects legacy `?token=`; SPA must fetch a single-use ticket from `/api/auth/ws-ticket` |
| Upload validation | Media-only (MIME prefix or suffix allowlist) else HTTP 415; 200MB limit; filename sanitized (path traversal/NUL stripped, 140-char cap); stored under `HERMES_HOME/dashboard-uploads/<date>/` with random token prefix; tests at `tests/hermes_cli/test_dashboard_chat_uploads.py` |
| Approval gateway (subset) | Command approval via `approval.request` → once/session/deny; sudo password and secret env-var values collected in UI and sent via `sudo.respond`/`secret.respond` — values held in plain React state until submit |
| Error display | Gateway `error` events and request failures render as a generic red banner + system message; attach errors render under the composer; tool failures render as error-status tool rows with auto-expanded detail |

### Specified, not built

- Typed error contract with nine codes — `missing_credential`,
  `unsupported_model_capability`, `invalid_asset_ref`,
  `provider_rejected_input`, `quota_exceeded`, `job_timeout`,
  `asset_upload_failed`, `sandbox_unavailable`, `approval_required` — each
  rendered as an error card with an actionable recovery path; "do not convert
  these into vague apologies" (`02` §Error Contract). No typed errors reach
  the UI today. The per-code recovery-action mapping is itself unspecified
  (see Open Questions).
- Approval required for spend / private-media / logged-in-account /
  local-command / publish actions, with durable pause/resume across refresh
  (`02` §Human Approval Gateway).

### Known holes

- Mobile users cannot answer approvals or clarifications: the inspector is
  the only host of `PendingPromptPanel` and is hidden below the `lg`
  breakpoint.
- Skill-internal `references/` protection
  (`docs/hermes-real-chat-agent-ui.md`) has no visible enforcement in the
  chat upload/attachment path.
- Per-user upload quota, auth requirements on `/api/chat/uploads` beyond the
  dashboard session, and which sessions a user may resume are unspecified.

## Acceptance Criteria

Verifiable today (regression bar for the implemented surface):

1. With `hermes dashboard --tui`, `/chat` renders; without the injected flag,
   `/chat` is absent.
2. Opening `/chat` creates a session via `session.create` and triggers no
   media generation.
3. Submitting text streams assistant output token-by-token
   (`message.start/delta/complete`), with a stop button that issues
   `session.interrupt`.
4. Uploading an image via the paperclip returns 200 from
   `POST /api/chat/uploads`, produces an attachment chip, and the next
   `prompt.submit` carries the real attachment marker. A non-media file
   returns 415. A >200MB body is rejected.
5. A clarify request renders choice buttons plus free text and resolves via
   `clarify.respond`; an approval renders once/session/deny and resolves via
   `approval.respond`.
6. `/chat?resume=<id>` restores the message history text of that session.
7. A failing tool call renders an auto-expanded error row in the Tools list.

Required for the creative layer (from the spec pack — not yet satisfiable):

8. Browser refresh during a media job must not lose the job; the resumed
   session shows active jobs (`02` §Acceptance).
9. A page refresh re-presents an outstanding approval request from durable
   state (`02` §Human Approval Gateway).
10. A failed provider request shows a typed error with a visible retry path;
    failed jobs remain inspectable (`02` §Acceptance, `06` §Launch Gates).
11. Uploaded media becomes a typed `media_input` asset, not plain prompt text
    (`00` §Top-Level Acceptance).
12. The UI streams thinking/status/tool/media events instead of freezing
    during long jobs (`00` §Top-Level Acceptance).
13. Generated media renders as an asset card in the transcript; the inspector
    can open the card and show model/job/prompt/input details (`03`
    §Acceptance).
14. The agent cannot claim completion without an event/artifact/ledger record
    (`02` §Acceptance).
15. No fake media URLs, no hardcoded job results, no accidental FAL/Comfy
    fallback when Atlas is selected (`06` §Launch Gates).
16. A vague "make a video" routes to the `video_generate` intent and asks at
    most one useful question (`04` §Clarification Rules).

## Non-Goals

- No auto-start generation on open; user intent drives execution
  (`docs/hermes-real-chat-agent-ui.md` §Non-Goals).
- No raw provider dashboards inside the chat surface (`01` §Non-Goals).
- No internal prompt templates exposed by default (`01` §Non-Goals).
- No fake run/status panel disconnected from real Hermes events (`01`
  §Non-Goals).
- No merged generic Assets page as part of this component (`01` §Non-Goals).
- No hardcoded prompts/jobs, no dashboard-plugin job endpoints, no fake Atlas
  responses (`hermes-real-chat-agent-ui.md` §Non-Goals).
- The inspector is not a second chat (`01` §Inspector).
- The legacy PTY `ChatSidebar.tsx` surface and the `standalone-chat-panel/`
  package are not specified here; whether they are replaced or kept in
  parallel is an open question, not a commitment of this spec.

## Open Questions

1. Media event source: should media cards render from a new
   `asset.ready`/`media_job.*` event family (gateway work, spec `02`
   mandates it) or from enriched `tool.complete` payloads (faster, less
   durable)? Nothing exists yet.
2. Selection model: how is the "currently selected job/asset" chosen and
   stored for the inspector — click target, URL state, or session state —
   and does it survive resume?
3. Approval decision sets: keep implemented `once/session/deny`, adopt spec
   `approve/edit/reject/respond`, or merge? "edit" (modify command/payload
   before approving) needs full UX design.
4. Does the new ChatPage replace the legacy PTY `ChatSidebar` surface, or do
   both ship?
5. Model picker source: Hermes profiles, the Atlas catalog
   (`plugins/*/atlas/catalog.py`), or the spec-only `ultra_model_catalog`?
   Is mid-session switching allowed, and how does it interact with
   workflow-router model selection?
6. Should `prompt.submit` gain a structured attachments field (typed
   `media_input` refs), and what is the migration path from text markers?
7. How should `thinking.delta`/`reasoning.delta` render in a consumer
   creative product, given the `01` non-goal of not showing internal prompt
   templates — phase label only, collapsible reasoning, or hidden?
8. What is the durable store for pending approvals/clarifications so a
   refresh re-presents them, and which RPC returns them on resume?
9. Is the hardcoded greeting ("lif, ...") meant to become a
   user-profile-driven personalized greeting, and what is the name source?
10. Whose responsibility is one-question clarification (`04` rules) — the
    workflow-router skill prompt, or enforced UI/gateway-side — and how is
    "only one question" verified?
11. Typed-error-to-recovery-action mapping: which concrete UI action pairs
    with each of the nine error codes (e.g. `missing_credential` →
    credentials deep link, `quota_exceeded` → upgrade link)?
12. Event vocabulary reconciliation: which side renames —
    `approval.requested/resolved` vs `approval.request/respond`, `tool.error`
    vs `tool.complete`-with-error — and where does the canonical table live?
13. Multi-file upload, drag-and-drop, and paste-image flows: in or out of
    the first creative release?
14. Transcript scalability: virtualization, scroll-lock, history pagination,
    and the 24-entry tool feed cap for long creative sessions.
15. Session naming: `session.title` RPC exists but nothing in the chat UI
    uses it — does the creative session expose rename/auto-title?
16. Mobile surface for pending approvals/clarifications, since the inspector
    is hidden below `lg`.
