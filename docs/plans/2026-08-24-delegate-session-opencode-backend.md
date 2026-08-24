# Plan: Configurable delegate_session Backends (Pi + OpenCode)

- artifact_contract: ce-unified-plan/v1
- artifact_readiness: implementation-ready
- execution: code
- plan_depth: standard
- risk: medium
- date: 2026-08-24
- branch: feat/delegate-session-opencode (on top of merged Pi work, head 9940f829df)

## Goal

Make `delegate_session` backend-configurable. Pi remains the default; OpenCode is
added as a second backend driven by the OpenCode HTTP server API (never TUI
scraping). Hermes supervises backend-native interactive questions and answers
OpenCode `question.asked` requests itself via the existing `run_oneshot`
auto-answer path, with zero user involvement.

## Settled decisions (from user instruction)

1. **Pi stays the default backend** — user-directed; rejected alternative:
   OpenCode-first or no default. Reason: preserves existing behavior.
2. **OpenCode backend uses the server/session HTTP API** (`opencode serve`) —
   user-directed ("server/session APIs rather than TUI scraping"); rejected:
   pty/TUI scraping. Reason: machine-readable, robust.
3. **API semantics preserved** — start/resume/send/steer/status/messages/list/stop
   unchanged; a `backend` parameter is additive. Rejected: a new tool name.
   Reason: no regression for Pi callers.
4. **Durable resume metadata is backend-aware** — metadata gains a `backend`
   field; resume reopens the correct backend. Rejected: separate stores.
5. **Hermes autonomously answers OpenCode questions** — rejected: surfacing to
   the user. Reason: user explicitly required no user intervention.
6. **Live e2e validation with installed opencode 1.18.18 is part of done** —
   rejected: hermetic-only validation. Reason: user requirement.

## Grounded API facts (probed against local opencode 1.18.18, OpenAPI /doc)

- `opencode serve --port N --hostname 127.0.0.1` → HTTP JSON API.
- `POST /session?directory=<cwd>` body `{title}` → `{id: "ses_...", ...}`.
- `POST /session/{id}/prompt_async` body `{parts:[{type:"text",text}]}` → HTTP 204,
  runs asynchronously. (There is no native "steer" for prompt_async.)
- `GET /question` → array of pending `QuestionRequest`:
  `{id:"que_..", sessionID, questions:[{question, header, options:[{label,description}], multiple, custom}], tool}`.
- `POST /question/{requestID}/reply` body `{answers:[[label,...], ...]}`
  (one answer-array per question, values are option labels) — `custom:true`
  allows a free-text label. `POST /question/{requestID}/reject` also exists.
- `POST /session/{id}/abort` (empty body), `GET /session/{id}/message`
  → array of `{info:{id,role,time,...}, parts:[{type:"text",text},...]}`.
- `GET /session/status` → `{}` on this build (unreliable); busy/idle must be
  tracked from message polling / active turn bookkeeping, not this endpoint.
- Agent/model come from user opencode config (cliproxyapi provider present).
- OpenCode permissions (`permission.asked`) are a separate mechanism; this work
  targets `question.*` (delegate sessions should run with a permission ruleset
  that does not block, or permission events surface as errors — documented
  limitation, not auto-approved in v1).

## Design

### Backend protocol (structural refactor of tools/delegate_session_tool.py)

Introduce `DelegateBackend` protocol with methods mapped from the existing
Pi-coupled call sites:

- `start(cwd, session_name) -> native_id` — begin/attach native session
- `run_prompt(text, timeout) -> {"text": str, "native_session_id": str, "duration_s": float}`
- `steer(text, timeout) -> response` — live course correction
- `abort(timeout)` — cancel active turn
- `get_messages(timeout) -> list` — recent transcript
- `close()` — release backend resources
- `is_dead()` — backend process/connection liveness (replaces `client._proc.poll()`)
- `pending_question()` — `None | {"method","question","options","created_at"}`

Pi backend: thin adapter over the existing `PiRPCClient` (behavior preserved
byte-for-byte at the tool layer; existing tests must pass unchanged).

OpenCode backend (`agent/opencode_client.py`, new):

- **Server lifecycle**: one lazily-spawned shared `opencode serve` per Hermes
  process (module-level singleton). Spawn with a free 127.0.0.1 port, env
  `HERMES_OPENCODE_BIN` override (default `opencode`), readiness = HTTP 200 on
  `/session` (poll ≤30s). Reference-counted close; refcount 0 terminates server.
- **start**: `POST /session?directory=<cwd>` with `title`.
- **send**: dispatch thread → `POST prompt_async`, then poll `GET
  /session/{id}/message` until a new assistant message with text parts appears
  after the user message (timeout = tool timeout; abort via `POST abort` on
  timeout or stop).
- **steer**: no native steer for prompt_async. Mirror the existing Pi semantics:
  if a turn is running, do NOT inject concurrently — degrade steer to a queued
  send for v1 and return `degraded_to_send` + note (same contract Pi already
  exposes when a turn ended). Documented limitation.
- **questions**: poll `GET /question` (same cadence as message polling, plus a
  dedicated lightweight poller while a turn runs). Filter by `sessionID`. For
  each pending request: call the shared Hermes auto-answer callback
  (`(method, question, options) -> str|None`), then `POST /question/{id}/reply`
  with `answers=[[answer]]` per question (free text allowed when `custom`;
  otherwise best label-match; `None` answer → `reject` — conservative fallback).
- **resume**: server is stateless across restarts but OpenCode persists sessions
  on disk; reopening = ensure server up + reuse stored `native_session_id`
  (messages endpoint works for persisted sessions; a missing session is a
  bounded error).
- **status**: derived from in-memory record + turn thread + `is_dead()`.

### Tool layer changes (tools/delegate_session_tool.py)

- New `backend` argument: `"pi"` (default) | `"opencode"`, resolved as
  `arg → HERMES_DELEGATE_SESSION_BACKEND env → "pi"`.
- Backend selection applies at start/resume; other actions look up the stored
  backend from the record/metadata (a mismatched explicit `backend` on control
  actions is a bounded tool_error).
- Registry/store: record gains `backend`; `_SESSIONS` keyed by session_id as
  today (IDs are UUIDs, collision-free across backends).
- Metadata v2: `{version:2, backend, session_id, native_session_id (renamed
  from pi_session_id; pi_session_id kept as read-alias), owner, cwd, created_at,
  updated_at}`. v1 files load with backend="pi".
- `list` includes `backend`; `_summary` renames `pi_session_id` →
  `native_session_id` (keep `pi_session_id` as duplicate field for one release
  for model-callers; tests assert both).
- Auto-answer: factor `_auto_answer_pi_question` into backend-neutral
  `_auto_answer_delegate_question(parent_agent, backend_name, method, question,
  options)`; wording generalized ("your coding delegate"), logic unchanged.
- `check_delegate_session_requirements`: true when pi OR opencode is available.
- Schema: add `backend` enum param; description generalized.

## Implementation units

- **U1** `agent/opencode_client.py` — `OpenCodeClient` implementing the backend
  surface (server singleton lifecycle, session create, prompt_async+poll,
  question poll/reply/reject, abort, messages, close, is_dead). Pure stdlib
  (`urllib.request`/`http.client`), no new deps. HTTP layer injectable for
  hermetic tests.
- **U2** `tools/delegate_session_tool.py` refactor — `DelegateBackend` protocol,
  Pi adapter wrapping `PiRPCClient`, backend resolution, store/metadata v2,
  generalized auto-answer, generalized schema/description.
- **U3** Tests:
  - `tests/tools/test_delegate_session_tool.py` — extend: Pi default regression
    (backend omitted → Pi path), backend=opencode routing with a fake backend,
    metadata v2 round-trip + v1 migration, list includes backend, mismatched
    backend error.
  - `tests/agent/test_opencode_client.py` — hermetic: fake HTTP transport
    (create/prompt 204/message growth/timeout abort), question reply payload
    shape (`answers=[[label]]`), reject fallback, server spawn/readiness with
    stub binary, refcounted close.
  - Pi compatibility: existing `tests/agent/test_pi_rpc_client.py` and
    `tests/tools/test_delegate_session_tool.py` pass unchanged (except additive
    assertions).
- **U4** Live e2e validation (manual script `scripts/validate_delegate_opencode.py`,
  not shipped as a pytest): real `opencode serve`, delegate goal that forces the
  OpenCode ask tool ("Use the ask tool to ask which color, then report the
  answer"), parent agent stub feeding auto-answer via `run_oneshot`; assert a
  `question.asked` was replied with a Hermes-generated answer and the session
  transcript contains it — no user interaction.

## Non-goals

- Auto-approving OpenCode `permission.asked` (separate mechanism; v1 surfaces
  permission-blocked turns as session errors).
- Native concurrent steer injection (OpenCode has none for prompt_async).
- Changing delegate_task semantics.

## Risks

- `/session/status` unreliable (`{}`) → busy tracking must be client-side. Mitigated by design.
- Question free-text requires `custom:true`; non-custom selects need exact label
  match → answerer prompt already enforces exact-option output; mismatch → reject fallback.
- Server singleton vs multi-cwd sessions: fine — sessions are per-directory query params.
