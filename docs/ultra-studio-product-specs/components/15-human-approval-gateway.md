# Human Approval Gateway

Status: partial — in-process approval machinery exists (command approval
with hardline blocking, a clarify gateway with timeouts and notification
callbacks, slash confirmation); the durable approval contract (typed
decisions, page-refresh survival, `approval.*` events) is spec-only.
Date: 2026-06-11

Sources:

- Docs: `docs/ultra-studio-product-specs/02-agent-runtime-contract.md`
  (§Human Approval Gateway, §Event Stream, §Error Contract),
  `01-product-surface.md` (§Required States: "Waiting for user"),
  `06-delivery-plan.md` (P2 item 7, P2 gate "Cost/private/publish actions
  require approval"), `00-index.md` (§Top-Level Acceptance)
- Code (verified this session): `tools/approval.py`
  (`_fire_approval_hook`, `set_current_session_key`,
  `_is_gateway_approval_context`, `detect_hardline_command`,
  `_check_sudo_stdin_guard`, approval key aliasing),
  `tools/clarify_gateway.py` (`register`, `wait_for_response`,
  `resolve_gateway_clarify`, `get_pending_for_session`,
  `mark_awaiting_text`, `get_clarify_timeout`, `register_notify`),
  `tools/slash_confirm.py`, `gateway/run.py`, `gateway/pairing.py`
  (approval-related handling), `agent/tool_guardrails.py`

## Purpose & Scope

Approval is required for actions that spend money, expose private media,
touch logged-in accounts, run local commands, or publish externally
(`02-agent-runtime-contract.md` §Human Approval Gateway). The agent must
pause and resume from durable state; a page refresh must not lose the
approval request.

Decision types: `approve`, `edit`, `reject`, `respond`.

Scope: approval triggers, the pause/resume contract, decision handling,
durability, events, and the relationship to clarification (ask-user
questions). Clarification and approval share plumbing (the clarify gateway)
but differ in intent: clarification fills missing fields
(`12-workflow-router.md` ask-once rules); approval authorizes consequences.

## Implementation Status

| Status | Item | Citation |
|---|---|---|
| Implemented | Command approval flow with per-session context keys and platform awareness | `tools/approval.py` (`set_current_session_key`, `_get_session_platform`, `_is_gateway_approval_context`) |
| Implemented | Hardline command detection (always-block class) and sudo-stdin guard | `tools/approval.py` (`detect_hardline_command`, `_hardline_block_result`, `_check_sudo_stdin_guard`) |
| Implemented | Approval pattern keys with aliasing (remember prior decisions per pattern) | `tools/approval.py` (`_legacy_pattern_key`, `_approval_key_aliases`, `_normalize_command_for_detection`) |
| Implemented | Approval lifecycle hooks for observers | `tools/approval.py` (`_fire_approval_hook`) |
| Implemented | Ask-user gateway: register a pending question, block with timeout, resolve from another channel, notify callbacks, per-session pending state | `tools/clarify_gateway.py` (`register`, `wait_for_response`, `resolve_gateway_clarify`, `register_notify`, `get_clarify_timeout`) |
| Implemented | Slash-command confirmation path | `tools/slash_confirm.py` |
| Implemented | Tool-level guardrails preceding approval | `agent/tool_guardrails.py` |
| Specified, not built | Typed decisions `approve / edit / reject / respond` as a uniform contract (today: approve/deny + free-text response paths) | `02-agent-runtime-contract.md` §Human Approval Gateway |
| Specified, not built | Durable approval requests (survive page refresh / gateway restart) — the clarify gateway state is in-process (module-level registry with timeouts) | same; `tools/clarify_gateway.py` structure |
| Specified, not built | `approval.requested` / `approval.resolved` gateway events to the web UI | `02-agent-runtime-contract.md` §Event Stream |
| Specified, not built | Trigger taxonomy beyond commands: spend-money, private-media, logged-in-account, publish-external | §Human Approval Gateway; today's machinery is command/tool-centric |
| Specified, not built | `approval_required` typed error | §Error Contract |
| Specified, not built | Web approval card UI ("Waiting for user" state) | `01-product-surface.md` §Required States; web chat renders ask-user cards (see `02-creative-chat-ui.md`), approval-specific cards planned |

## User Entry Points

- Approval card in chat when the agent requests authorization (planned
  rendering; plumbing exists for messaging platforms via the gateway).
- Inline command-approval prompts in TUI/CLI sessions (implemented path
  through `tools/approval.py`).
- Pending-approval indicator on task rows ("Waiting" status,
  `07-tasks-session-history.md` State Machine).
- Approval resolution from a different surface than the request (the
  clarify gateway supports cross-channel resolve:
  `resolve_gateway_clarify`).

## Feature List

| Feature | Status |
|---|---|
| Pause agent execution pending a human decision | Implemented (blocking `wait_for_response` with timeout) |
| Hardline blocks that approval cannot override | Implemented (`detect_hardline_command`) |
| Remembered approvals per command pattern | Implemented (pattern keys + aliases) |
| Session-scoped pending question state | Implemented (`get_pending_for_session`, `clear_session`) |
| Notification callbacks on pending approvals | Implemented (`register_notify`) |
| Typed decision set incl. `edit` (modify-then-approve) | Planned |
| Durable approval records surviving restarts | Planned |
| `approval.requested/resolved` events to web UI | Planned |
| Trigger taxonomy: money / private media / accounts / local exec / publish | Planned (local exec exists; the rest need integration points in media job, publish, and browser flows) |
| Approval audit trail (who, when, what changed) | Planned (pairs with `16-observation-provenance-ledger.md`) |
| Timeout policy with safe default deny | Implemented mechanically (`get_clarify_timeout`); product policy per trigger class planned |

## State Machine

Approval request lifecycle (target contract):

```text
created -> pending -> resolved(approve)
                   -> resolved(edit)      (payload modified, then proceeds)
                   -> resolved(reject)
                   -> resolved(respond)   (free-text answer back to agent)
pending -> expired   (timeout policy; default deny for consequential actions)
```

Agent-side:

```text
running -> paused(awaiting_approval) -> resumed (with decision attached)
                                      -> aborted (reject/expired)
```

Rules:

- `pending` must be durable: re-rendered after refresh, re-deliverable
  after gateway restart (planned; today restart drops in-process state).
- `edit` returns the modified payload to the exact decision point — the
  agent must use the edited version, not re-derive its own.
- Hardline-blocked actions never enter `pending`; they are refused outright
  (`_hardline_block_result` behavior).
- Expiry of a money/publish approval is a deny, never an implicit approve.

## APIs & Events

Implemented (in-process):

- `register(...) -> clarify_id`, `wait_for_response(clarify_id, timeout)`,
  `resolve_gateway_clarify(clarify_id, response)`,
  `get_pending_for_session(session_key)`, `mark_awaiting_text`,
  `register_notify/unregister_notify` — `tools/clarify_gateway.py`.
- Approval hook firing for lifecycle observers
  (`_fire_approval_hook(hook_name, **kwargs)`).

Planned (gateway contract):

- Events `approval.requested` (typed payload: trigger class, summary of the
  action, editable fields, expiry) and `approval.resolved` (decision,
  decider, edited payload hash) — `02-agent-runtime-contract.md`
  §Event Stream.
- Typed error `approval_required` returned by tools that hit an approval
  gate in a non-interactive context (cron/scheduled runs).

```http
POST /api/approvals/{approval_id}/resolve   # {decision, edited_payload?}
GET  /api/approvals?session_id=&status=pending
```

## Data Model

Implemented: in-memory `_ClarifyEntry` registry keyed by clarify id with
session key, timestamps, awaiting-text flag (`tools/clarify_gateway.py`);
approval pattern decisions persisted per session/config in the approval
layer.

Planned durable entity:

```text
approval_requests
- approval_id
- session_id, run_id, tool_call_id
- trigger_class: spend_money | private_media | account_access
                | local_exec | publish_external
- action_summary          (human-readable, no secrets)
- payload_ref             (what will execute if approved)
- editable_fields[]
- status: pending | approved | edited | rejected | responded | expired
- decision_by, decided_at, expiry_at
- edited_payload_hash     (when decision = edited)
```

## UI Behavior

- An approval card renders in the transcript at the decision point: action
  summary, trigger class badge, cost estimate where applicable, and the
  four decision buttons; `edit` opens the editable fields inline.
- The card persists across refresh (durable `pending`), shows expiry
  countdown, and collapses into a resolved record (decision + who + when)
  after resolution.
- The inspector mirrors the pending approval's context (missing field /
  consequence details), per `01-product-surface.md` §Required States
  ("Waiting for user": center renders structured question; inspector shows
  missing field context).
- Approvals never render raw secrets or full credentials in the summary.
- Non-interactive surfaces (cron) surface `approval_required` as a task
  state, not a silent skip.

## Permissions & Error Handling

- Only the session owner (or explicitly authorized workspace roles) can
  resolve an approval; resolutions record the decider.
- Remembered approvals apply per pattern and per session scope; a
  remembered command approval never generalizes to money/publish triggers.
- Typed errors: `approval_required` (gate hit, no interactive channel),
  `approval_expired`, `approval_not_found` (stale resolve).
- Failure of the approval store must fail closed for consequential actions
  — execution does not proceed on a lost request (aligned with the
  TokenRouter fail-closed posture, `hermes-tokenrouter-credential-flow.md`
  §失败行为).

## Acceptance Criteria

- A media job that would spend money pauses with an approval card; refresh
  re-renders the same card; approving resumes from durable state
  (`02-agent-runtime-contract.md`: "A page refresh must not lose the
  approval request").
- `edit` decisions provably alter the executed payload (diff visible in
  inspector / ledger).
- Reject and expiry both abort the action with a typed, visible outcome.
- Hardline commands are blocked even if the user clicks approve (no
  override path).
- A scheduled (non-interactive) run hitting a gate ends in
  `approval_required` task state, not execution and not silence.
- P2 gate holds: "Cost/private/publish actions require approval"
  (`06-delivery-plan.md`).

## Non-Goals

- Multi-step approval chains / multi-party sign-off (single decider in
  scope).
- Replacing tool guardrails: guardrails decide what is forbidden;
  approvals decide what is permitted-with-consent. Hardline blocks stay
  non-overridable.
- Approval for routine read-only tool calls (would create approval
  fatigue; trigger taxonomy is consequence-based).
- Clarification UX redesign (ask-user questions are routed by the router's
  ask-once rules; this component only shares transport).

## Open Questions

1. Durability backend: where do `approval_requests` live (gateway DB?), and
   how do messaging-platform sessions (Telegram/WhatsApp via gateway
   platforms) get the same durable card semantics?
2. Default timeout per trigger class — command approvals today use the
   clarify timeout (`get_clarify_timeout`); money/publish likely need
   longer expiry plus deny-on-expire. Values unspecified.
3. `respond` vs clarification: is `respond` just a clarify answer attached
   to an approval, or a distinct decision with different resume semantics?
4. Cost estimation source for spend-money cards before TokenRouter usage
   metering exists.
5. Who can resolve in shared/multi-user gateway sessions
   (`gateway/session.py` `is_shared_multi_user_session`) — requester-only,
   or any participant?
6. Does an `edit` decision require recompile (`13-prompt-compiler.md`) when
   the edited fields affect the payload, and who re-validates constraints?
