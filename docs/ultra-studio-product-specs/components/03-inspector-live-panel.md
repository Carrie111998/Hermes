# Inspector / Live Panel

Status: partial — a generic chat/session inspector is implemented; the creative-asset
inspector (selection, asset preview, QA, lineage, download, element/character actions)
is spec-only with zero implementation.
Date: 2026-06-10

Sources:

- Docs: `docs/ultra-studio-product-specs/00-index.md`,
  `01-product-surface.md`, `02-agent-runtime-contract.md`,
  `03-media-asset-contract.md`, `05-memory-marketplace-files.md`,
  `06-delivery-plan.md`
- Code: `web/src/components/chat/ChatInspector.tsx`,
  `web/src/components/chat/PendingPromptPanel.tsx`,
  `web/src/components/chat/contracts.ts`, `web/src/components/ToolCall.tsx`,
  `web/src/hooks/useGatewayChat.ts`, `web/src/lib/gatewayClient.ts`,
  `web/src/pages/ChatPage.tsx`, `tui_gateway/server.py`,
  `tools/image_generation_tool.py`, `tools/video_generation_tool.py`,
  `hermes_cli/ultra_studio_skills.py`, `skills/creative/media-qa/SKILL.md`,
  `skills/creative/prompt-repair/SKILL.md`

## Purpose & Scope

The Inspector / Live Panel is the right rail of Ultra Studio. Per
`01-product-surface.md` it is a context panel for the currently selected job,
asset, or tool run. It is not a second chat. It is closer to an IDE inspector or
a Figma properties panel.

The target spec scope:

- Current job status and progress.
- Provider/model and input constraints.
- Selected asset preview: prompt, seed, dimensions, duration, lineage.
- QA result with observed evidence.
- Download/export actions.
- Convert to Element. Create Character.
- Retry/repair plan on failure.

What ships today is narrower: a fixed 320px aside on the chat page that shows the
session card, pending input prompts (clarify/approval/sudo/secret), and a live
tool-call feed. There is no selection model, no asset or media-job concept, no
preview, no QA panel, no lineage, no download, and no element/character actions
anywhere in `web/src` or the gateway.

Boundary (per `05-memory-marketplace-files.md`): the inspector is for the
selected object. Marketplace, Files, Memory, and Tasks browse durable workspace
state. The inspector never becomes a general browser.

## Implementation Status

| Capability | Status | Source |
|---|---|---|
| Session card: connection badge, model, session id, status line | Implemented | `web/src/components/chat/ChatInspector.tsx` |
| Status priority `error ?? credential_warning ?? currentStatus` | Implemented | `ChatInspector.tsx` line 62 |
| Live tool-call feed (expandable rows, args/preview/summary/error/diff, elapsed timer) | Implemented | `ChatInspector.tsx`, `web/src/components/ToolCall.tsx`, `web/src/hooks/useGatewayChat.ts` |
| Tool feed cap: last 24 entries | Implemented | `useGatewayChat.ts` `TOOL_LIMIT = 24` (lines 22, 181) |
| "Input needed" panel: clarify / approval / sudo / secret | Implemented | `web/src/components/chat/PendingPromptPanel.tsx` |
| Tools empty state ("no tool calls yet") | Implemented | `ChatInspector.tsx` |
| Generation backend the inspector would describe (`image_generate`, `video_generate`, Atlas plugins) | Implemented (synchronous tools, not durable jobs) | `tools/image_generation_tool.py`, `tools/video_generation_tool.py`, `plugins/{image,video}_gen/atlas` |
| QA/repair capability as prompt-layer skills (chat text only) | Implemented | `skills/creative/media-qa/SKILL.md`, `skills/creative/prompt-repair/SKILL.md`, `hermes_cli/ultra_studio_skills.py` |
| Selection model (selected job / asset / tool run) | Specified, not built | `01-product-surface.md` "Right: Inspector / Live Panel" |
| Media job status/progress from `media_job.*` events | Specified, not built | `02-agent-runtime-contract.md` "Event Stream" |
| Asset preview with prompt/seed/dimensions/duration/lineage | Specified, not built | `01-product-surface.md`, `03-media-asset-contract.md` |
| Provider/model constraints display (`ultra_media_constraints_get`) | Specified, not built | `03-media-asset-contract.md` "Required Job Tools" |
| QA panel: observed facts vs inferred quality | Specified, not built | `03-media-asset-contract.md` "QA" |
| Download/export using real storage URLs | Specified, not built | `03-media-asset-contract.md` "Acceptance" |
| Convert to Element / Create Character actions | Specified, not built | `01-product-surface.md`; `06-delivery-plan.md` P1 item 7 |
| Retry/repair plan card on failure | Specified, not built | `01-product-surface.md`; `06-delivery-plan.md` P1 item 5 |
| Failed-state view with typed provider error + logs | Specified, not built | `01-product-surface.md` "Required States"; `02-agent-runtime-contract.md` "Error Contract" |
| Refresh-safe approvals from durable state | Specified, not built (current approvals are in-process `threading.Event`) | `02-agent-runtime-contract.md`; `tui_gateway/server.py` `_block()` |
| Mobile/narrow-viewport inspector | Open gap (spec silent; code hides panel below `lg`, including pending prompts) | `ChatInspector.tsx` line 65 |
| Thinking/reasoning phase display | Open gap (gateway emits `thinking.delta`/`reasoning.delta`; web hook discards them) | `tui_gateway/server.py`, `useGatewayChat.ts` |
| Tool history beyond 24 entries; tool feed rehydration after `session.resume` | Open gap | `useGatewayChat.ts` |
| Rendering for `subagent.*` and `background.complete` events | Open gap (emitted, unspecified, unrendered) | `tui_gateway/server.py` |

Planned behavior in this document is never shipped behavior. Anything below
tagged Planned or Gap does not exist in code as of 2026-06-10.

## User Entry Points

| Entry | Status | Behavior |
|---|---|---|
| Open chat page at `lg+` viewport | Implemented | Inspector is always present as the right rail of `ChatPage`; no action to open or close it (`web/src/pages/ChatPage.tsx`). |
| Agent requests input (clarify/approval/sudo/secret) | Implemented | "Input needed" card appears in the inspector; it is the only place to answer (`PendingPromptPanel.tsx`). |
| Agent runs any tool | Implemented | Tool call appears live in the tools feed; user expands rows to inspect args/results/errors (`ToolCall.tsx`). |
| Resume session via `?resume=<id>` URL param | Implemented | Inspector shows resumed session id/model, but empty tools list and no pending prompt (`ChatPage.tsx`, `useGatewayChat.ts`). |
| Click "inspect" on a media/asset card | Planned | Inspector opens the asset with model/job/prompt/input details (`03-media-asset-contract.md` "Asset Card UI", "Acceptance"). |
| Select a running job | Planned | Inspector shows job details, model, inputs, progress (`01-product-surface.md` "Required States", Creating row). |
| Ask "Why did this fail?" / "Download this" | Planned | Inspector is the answer surface: job, status, error, QA, download (`01-product-surface.md` "Main Jobs"). |
| Inspector actions: Download/export, Convert to Element, Create Character, Retry with repair plan | Planned | `01-product-surface.md` "Right: Inspector / Live Panel". |

## Feature List

| Feature | Tag | Source |
|---|---|---|
| Session card: connection state badge (idle/connecting/live/closed/error), short model name, session id, current status line | Implemented | `ChatInspector.tsx` |
| Status text priority: error > credential_warning > currentStatus | Implemented | `ChatInspector.tsx` line 62 |
| Live tool-call feed: tool.start/progress/complete as expandable rows with name, context/args, streaming preview, result summary, error, inline diff, elapsed timer; error rows auto-expand | Implemented | `ChatInspector.tsx`, `ToolCall.tsx`, `useGatewayChat.ts` |
| "Input needed" panel: clarify (free text + choice buttons), approval (approve once / approve session / deny, command preview), sudo password, secret value | Implemented | `PendingPromptPanel.tsx` |
| Tools empty state | Implemented | `ChatInspector.tsx` |
| Backend generation tools: `image_generate` / `video_generate` with Atlas provider plugins | Implemented | `tools/*.py`, `plugins/{image,video}_gen/atlas` |
| Backend QA/repair as prompt-layer skills (media-qa scorecard, prompt-repair retry plan) — output is chat text, not inspector data | Implemented | `skills/creative/media-qa`, `skills/creative/prompt-repair` |
| Selection model: inspector bound to currently selected job, asset, or tool run | Planned | `01-product-surface.md`; no selection state in code |
| Media job status + progress from `media_job.created/updated` | Planned | `02-agent-runtime-contract.md`; events and job system do not exist |
| Selected asset preview with prompt, seed, dimensions, duration, lineage | Planned | `01-product-surface.md`, `03-media-asset-contract.md` |
| Provider/model + input constraints display (`ultra_media_constraints_get`) | Planned | `03-media-asset-contract.md` |
| QA result panel separating observed evidence from inferred quality | Planned | `03-media-asset-contract.md` "QA" |
| Download / export using real storage URLs | Planned | `03-media-asset-contract.md` "Acceptance" |
| Convert to Element action | Planned | `01-product-surface.md`; `06-delivery-plan.md` P1 item 7 |
| Create Character action | Planned | `01-product-surface.md`; `06-delivery-plan.md` P1 item 7 |
| Retry / repair plan surface on failed generation, fed by prompt-repair | Planned | `01-product-surface.md`; `06-delivery-plan.md` P1 item 5 |
| Failed-state view with typed provider error + logs | Planned | `01-product-surface.md` "Required States"; `02-agent-runtime-contract.md` "Error Contract" |
| "Waiting for user" state with missing-field context (today only raw question text shows) | Planned | `01-product-surface.md` "Required States" |
| Approval requests surviving page refresh via durable state | Planned | `02-agent-runtime-contract.md`; current code loses them on refresh |
| Mobile/narrow-viewport inspector (panel and pending prompts hidden below `lg`, no fallback) | Gap | `ChatInspector.tsx` line 65; spec silent |
| Thinking/reasoning phase display ("Thinking" state row); gateway events exist, hook discards | Gap | `01-product-surface.md`; `useGatewayChat.ts` |
| Tool history beyond 24 entries; tool feed hydration after `session.resume` | Gap | `useGatewayChat.ts`; neither spec'd nor implemented |
| Inspector behavior for `subagent.*` and `background.complete` events | Gap | `tui_gateway/server.py`; unspecified and unrendered |

## State Machine

The inspector itself holds no persisted state. It mirrors four state machines.
Three are implemented; the asset/job machines are planned.

### Connection state (implemented)

`web/src/lib/gatewayClient.ts`; surfaced as the inspector badge via
`STATE_LABEL`/`STATE_TONE` in `ChatInspector.tsx`.

```text
idle -> connecting -> open -> closed | error
```

Triggered by: page load and the Reconnect button (user), WebSocket lifecycle
(transport).

### Tool call state (implemented)

`useGatewayChat.ts`, `ToolCall.tsx`. Triggered by gateway events emitted by the
agent runtime.

| From | To | Trigger |
|---|---|---|
| (none) | `running` | `tool.start` |
| `running` | `running` (preview update) | `tool.progress` |
| `running` | `done` | `tool.complete` without error payload |
| `running` | `error` | `tool.complete` with error payload |

### Pending prompt lifecycle (implemented)

| Step | Trigger | Effect |
|---|---|---|
| Prompt raised | gateway `clarify.request` / `approval.request` / `sudo.request` / `secret.request` | `pendingPrompt` set, `running=true`, card renders |
| Prompt answered | user responds via respond RPC | `pendingPrompt` cleared, status becomes `running` or `denied` |
| Interrupt | user `session.interrupt` | `_clear_pending` releases the owning session's prompts; status `interrupted` |
| Timeout | server-side (clarify 300s, sudo 120s) | blocking `threading.Event` returns empty answer |

Server side is an in-process blocking call (`_block()` in
`tui_gateway/server.py` ~lines 728-740). State is lost on page refresh; see
Permissions & Error Handling.

### Server session live status (implemented)

`_session_live_status` in `tui_gateway/server.py` (~line 2500):
`waiting` (pending prompt) | `starting` (agent not ready) | `working` (running) |
`idle`.

### Required product states (planned)

`01-product-surface.md` "Required States" defines six per-state inspector
behaviors. Only partial mapping exists today (status text + pending prompt).

| State | Inspector must show | Today |
|---|---|---|
| Empty | Nothing selected | Approximated by "no session" / empty tools |
| Thinking | Current reasoning phase | Not rendered (events discarded) |
| Waiting for user | Missing-field context | Raw question text only |
| Creating | Job details, model, inputs | Not built |
| Complete | Asset details and actions | Not built |
| Failed | Provider error, logs, repair plan | Not built |

### Asset and job lifecycles the inspector must mirror (planned)

From `03-media-asset-contract.md`. No code exists.

```text
Asset:  uploading -> processing -> ready -> archived
        failure states: failed | revoked | deleted
Job:    job.created -> job.running -> job.succeeded -> asset.processing -> asset.ready
```

The full `MediaJob.status` enum (queued? cancelled? timed_out?) and the
cancel/retry transitions are unspecified; see Open Questions.

## APIs & Events

### WS JSON-RPC methods used by the inspector surface (implemented)

Transport: JSON-RPC over WebSocket at `/api/ws`
(`web/src/lib/gatewayClient.ts`; handlers in `tui_gateway/server.py`).

| Method | Payload | Used for |
|---|---|---|
| `session.create` | — | New session |
| `session.resume` | `{session_id}` | Returns `{session_id, resumed, message_count, messages, info}` only; does not re-emit pending prompts or replay tool entries |
| `prompt.submit` | `{session_id, text}` | Send user message |
| `session.interrupt` | — | Clears pending prompt |
| `clarify.respond` | `{request_id, answer}` | Answer clarify card |
| `approval.respond` | `{session_id, choice, all}` | choices `once`/`session`/`deny`; `all: choice==='session'` |
| `sudo.respond` | `{request_id, password}` | Sudo card |
| `secret.respond` | `{request_id, value}` | Secret card |
| `image.attach`, `input.detect_drop` | — | Chat input, adjacent to inspector |

### Gateway events consumed by inspector state (implemented)

From `useGatewayChat.ts`:

| Event | Payload (relevant fields) |
|---|---|
| `session.info` | `{cwd?, model?, provider?, profile_name?, credential_warning?}` |
| `status.update` | `{text, kind}` |
| `tool.start` | `{tool_id, name, context}` |
| `tool.progress` | `{tool_id\|name, preview}` |
| `tool.complete` | `{tool_id, summary, error, inline_diff}` |
| `clarify.request` | `{request_id, question, choices}` |
| `approval.request` | `{command, description}` |
| `sudo.request` | `{request_id}` |
| `secret.request` | `{request_id, env_var, prompt}` |
| `error` | `{message}` (free text, untyped) |

### Emitted but unconsumed by the inspector (implemented backend, gap frontend)

`tui_gateway/server.py` `_emit()` also produces: `thinking.delta`,
`reasoning.delta`, `reasoning.available`, `tool.generating`,
`background.complete`, `subagent.*`, `message.start/delta/complete`,
`skin.changed`. None are handled by `useGatewayChat.ts`.

### Proposed events and tools (planned, zero code)

From `02-agent-runtime-contract.md` "Event Stream" and
`03-media-asset-contract.md` "Required Job Tools". A repo-wide search excluding
docs returns no implementation.

| Surface | Items |
|---|---|
| Events | `media_job.created`, `media_job.updated`, `asset.ready`, `approval.requested`, `approval.resolved` |
| Job tools | `ultra_media_job_create`, `ultra_media_job_status`, `ultra_media_job_cancel`, `ultra_media_job_retry`, `ultra_media_job_finalize`, `ultra_media_constraints_get` |

No read API for fetching a job or asset by id exists or is specified; whether
the inspector fetches on demand or accumulates from the event stream only is an
open question.

### Auth for the event channel (implemented)

Single-use ticket via `GET /api/auth/ws-ticket` when `__HERMES_AUTH_REQUIRED__`;
otherwise a session token query param (`web/src/lib/gatewayClient.ts`).

## Data Model

### Implemented (frontend state, not persisted)

```ts
// web/src/components/ToolCall.tsx
ToolEntry: {
  kind: 'tool', id: string, tool_id: string, name: string,
  context?: string, preview?: string, summary?: string,
  error?: string, inline_diff?: string,
  status: 'running' | 'done' | 'error',
  startedAt: number, completedAt?: number
}

// web/src/components/chat/contracts.ts
WebChatSessionInfo: { cwd?, model?, provider?, profile_name?, credential_warning?: string }

WebChatPendingPrompt:
  | clarify  { requestId, question, choices?: string[] }
  | approval { command, description }
  | sudo     { requestId, password }
  | secret   { requestId, envVar, prompt, value }

// web/src/lib/gatewayClient.ts
ConnectionState: 'idle' | 'connecting' | 'open' | 'closed' | 'error'
```

All of the above live in React state. Nothing the inspector shows is persisted
client-side; a refresh or Reconnect wipes it.

### Planned (no code, field types unspecified)

From `03-media-asset-contract.md`:

| Entity | Fields | Persistence |
|---|---|---|
| `MediaJob` | `job_id, session_id, run_id, tool_call_id, provider, model, media_type, mode, status, input_assets, prompt, negative_prompt, provider_constraints, seed, tokenrouter_decision_id, output_assets, error` | Durable store required; none exists |
| Asset (typed) | Types: `media_input, image_job, video_job, audio_job, element, character, soul_id, task_file` | None exists |
| Lineage record | parent asset ids, source job id, provider job id, model/endpoint, prompt hash, seed/params, user/session/run, output asset ids | None exists |
| QA result | Observed facts (downloadable, exists, duration/dimensions, thumbnail, job succeeded) vs inferred (prompt alignment, style fit, character consistency, readability, defects); media-qa skill adds a 0-5 scorecard with no serialized schema | None exists |

Note: `tokenrouter_decision_id` has no producer — TokenRouter is a research
artifact only (`docs/hermes-tokenrouter-credential-flow.md`); keep-or-drop for
P0 is an open question.

## UI Behavior

Implemented behavior, all from code:

- The inspector is a fixed-width (`w-80`, 320px) right aside inside `ChatPage`,
  left-bordered. It renders only at `lg+` viewports: the `<aside>` className is
  `hidden w-80 ... lg:flex` (`ChatInspector.tsx` line 65). Below `lg` it is
  fully hidden — including all pending prompts — with no drawer or sheet
  fallback.
- The session card always renders, showing "no session" when none exists. The
  status line shows `error ?? credential_warning ?? currentStatus` with a teal
  dot icon.
- The tools card scrolls independently (`min-h-0 overflow-y-auto`) and shows a
  newest-last list of up to 24 rows. Each row header shows name plus a status
  word colored by state (error=red, running=amber, done=teal). Clicking toggles
  the body with sections for context/streaming/diff/result/error. Error rows are
  expanded by default. Running rows show a pulsing dot and live elapsed time
  updating every 500ms. Resumed/historical tools with `startedAt === 0` hide the
  elapsed badge.
- `PendingPromptPanel` appears between the session and tools cards with amber
  "Input needed" styling. Approval shows the command in a scrollable `<pre>` and
  three buttons (approve once / approve session / deny). Clarify shows choice
  buttons plus a free-text input. Sudo and secret are password inputs.
- Answering clarify appends the answer as a user message in the center
  transcript. Deny sets status `denied`. Interrupt clears the pending prompt and
  sets status `interrupted`.
- The Reconnect button (ChatPage header) resets tools, pendingPrompt, and the
  session id, and creates a fresh `GatewayClient` — inspector tool history is
  wiped (`useGatewayChat.ts` `reconnect()`).
- Chat errors render as a banner in the center column and as inspector status
  text simultaneously.

Specified but absent UI (planned): asset preview pane, job progress bar, QA
evidence section, download/export buttons, Convert to Element / Create Character
buttons, repair-plan card, typed-error recovery actions.

Spec constraints on any future design (`01-product-surface.md` "Non-Goals"):
not a second chat; no raw provider dashboards; no internal prompt templates by
default; no fake status panel disconnected from real Hermes events.

## Permissions & Error Handling

Implemented:

- WS connection requires auth. Gated mode uses a single-use ticket from
  `/api/auth/ws-ticket`; legacy `?token=` is rejected when gated; a missing
  session token throws "Session token not available" and sets `state=error`
  (`web/src/lib/gatewayClient.ts`).
- Command approvals stream into the inspector via `approval.request` and resolve
  with `once`/`session`/`deny`; "approve session" sends `all: true`. The spec's
  decision set `approve/edit/reject/respond`
  (`02-agent-runtime-contract.md` "Human Approval Gateway") is not implemented;
  `edit` has no UI or RPC at all.
- Approvals are not refresh-safe. The server blocks on an in-process
  `threading.Event` and `session.resume` does not re-emit pending prompts, so a
  page refresh loses the approval/clarify card. This violates
  `02-agent-runtime-contract.md` ("a page refresh must not lose the approval
  request").
- Pending prompts time out server-side (clarify 300s, sudo 120s) returning empty
  answers. Interrupt releases only the owning session's prompts
  (`_clear_pending` in `tui_gateway/server.py`).
- Sudo passwords and secret env-var values are typed into the inspector and sent
  over the WS RPC. Values are held in React state in plaintext until submit
  (`PendingPromptPanel.tsx`). No spec statement exists on masking or persistence
  rules beyond `input type=password`.
- Errors are untyped strings: the gateway `error` event carries `{message}`; the
  UI shows a red banner in the center column, a system message, and inspector
  status text. No recovery actions.
- `credential_warning` in `session.info` surfaces missing-credential conditions
  as inspector status text — the only implemented hint toward the spec's
  `missing_credential` typed error.

Planned:

- Typed error contract (`02-agent-runtime-contract.md` "Error Contract"):
  `missing_credential`, `unsupported_model_capability`, `invalid_asset_ref`,
  `provider_rejected_input`, `quota_exceeded`, `job_timeout`,
  `asset_upload_failed`, `sandbox_unavailable`, `approval_required`. The
  inspector Failed state needs the type to show recovery actions. None of the
  nine codes are produced or consumed today.
- Privacy constraint: media cards and the inspector must not expose internal
  filesystem paths by default (`03-media-asset-contract.md` "Asset Card UI").
  The current tool feed does show raw paths in tool context strings, which will
  conflict once creative jobs run through it.

## Acceptance Criteria

Implemented today (verifiable now):

1. At a `lg+` viewport, the chat page renders the inspector with session card,
   and the badge transitions idle -> connecting -> live on connect.
2. A tool call by the agent appears in the tools feed as `running` and resolves
   to `done` or `error`; an error row is expanded by default and shows the error
   text.
3. The tools feed never shows more than 24 entries.
4. An `approval.request` renders the command and three buttons; `deny` sets
   status `denied`; "approve session" sends `approval.respond` with `all: true`.
5. A clarify answer appears as a user message in the transcript and clears the
   card.

Target spec (each currently fails; engineering done-when):

6. Clicking "inspect" on any asset card opens that asset in the inspector with
   model, job, prompt, and input details (`03-media-asset-contract.md`
   "Acceptance").
7. The inspector shows job/model/input/output details for the selected job
   (`06-delivery-plan.md` P0 acceptance) — driven by real `media_job.*` events,
   never by hardcoded results (Launch Gates: no fake media URLs, no hardcoded
   job results).
8. Failed jobs remain inspectable: a failed job shows a typed error, logs, and a
   repair plan; failed provider calls stay visible.
9. The QA panel renders observed facts and inferred quality as visually distinct
   sections; no inferred claim renders without an observation source or user
   review.
10. The download action resolves to a real storage/object URL or local
    materialization; no fabricated URL.
11. Convert to Element and Create Character are available from a selected
    eligible asset and produce the corresponding typed asset.
12. A page refresh during a pending approval re-renders the approval card from
    durable state.
13. The inspector shows the asset preview with prompt, seed,
    dimensions/duration, and lineage for the selected asset.
14. Top-level gate (`00-index.md`): the inspector shows the current job,
    selected asset, QA evidence, download, element creation, and character
    creation.

Test status: no frontend tests cover inspector rendering, event reduction, or
pending-prompt flows (no `*.test.*` files under `web/`). The delivery plan
requires frontend build/typecheck and a gateway event smoke test; no
inspector-specific acceptance tests are defined yet.

## Non-Goals

From `01-product-surface.md` "Non-Goals" and `05-memory-marketplace-files.md`:

- Not a second chat. The inspector never accepts free-form conversation.
- No raw provider dashboards in the main UI.
- No internal prompt templates shown by default.
- No fake run/status panel disconnected from real Hermes events.
- Not a workspace browser. Marketplace, Files, Memory, and Tasks own browsing of
  durable state; the inspector is scoped to the selected object.
- No internal filesystem paths exposed by default
  (`03-media-asset-contract.md` "Asset Card UI").

## Open Questions

1. Selection mechanism and priority: auto-select the latest job vs explicit
   click; does tool-run selection share the same panel as asset selection? No
   selection event or store is defined in spec or code.
2. Data source: does the inspector fetch on demand (a job/asset read API, none
   specified) or accumulate from the event stream only — and what is the
   reconciliation story after refresh/resume?
3. Full `MediaJob.status` enum and cancel/retry transitions: `queued`,
   `cancelled`, `timed_out` are unmentioned in `03-media-asset-contract.md`.
4. Structured schemas for QA results and repair plans: media-qa and
   prompt-repair currently emit prose; the inspector needs serialized data.
5. Approval durability: keep the in-process `once/session/deny` protocol or
   implement the spec's durable `approve/edit/reject/respond` gateway?
6. Mobile layout: drawer/bottom-sheet inspector vs hiding it; where do pending
   prompts go on small screens?
7. Eligibility rules and flows for Convert to Element / Create Character:
   "when eligible" is undefined; `docs/hermes-soulid-element-asset-model.md` is
   research-only.
8. Download path: proxy through the gateway vs direct Atlas object URLs (auth,
   expiry, local materialization).
9. `tokenrouter_decision_id`: TokenRouter has zero code — keep the field in the
   inspector contract or drop it for P0?
10. Should the inspector render thinking/reasoning deltas (events exist) or only
    coarse `status.update` phases per the "Thinking" state row?
11. Tool feed retention: is 24 entries enough, and should tool history rehydrate
    on `session.resume`?
12. Which inspector actions require explicit approval before executing? Retry
    spends money (`06-delivery-plan.md` open question: "Which actions require
    explicit approval before first launch?").
13. Repair-plan rendering and Retry semantics: new job vs same `job_id`, and
    whether retry requires cost confirmation.
