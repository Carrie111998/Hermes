# Parent-agent resolver for delegated command approvals

Status: implementation-ready design; no production wiring in this branch.

## Root cause

The approval system has one authority key: `session_key`.

1. Desktop/TUI registers one user-facing notify callback for the parent session (`register_gateway_notify(key, ...)`).
2. `delegate_task` copies the parent's ContextVars into worker threads.
3. `delegated_child_context(child_session_id)` isolates the durable child session id, but it does not replace the approval-specific session key.
4. The child guard therefore finds the parent's notify callback, queues under the parent session key, and emits `approval.request` to Desktop.
5. `approval.respond` can only resolve FIFO by that same session key. The parent model has neither a pending-request capability nor a resolver operation.

The runnable reproduction is `tests/tools/test_delegated_parent_approval_gap.py`.

Existing machinery that should be reused:

- `delegate_tool._active_subagents` already binds a child to the commissioning UI session and to unforgeable in-process transport/session-record identities for steering.
- `tools.async_delegation` already injects cache-safe fresh internal turns while the parent is idle.
- `delegate_task` already exists in the parent schema, so a narrow resolver operation can extend it without adding another core tool to every prompt.

## Security invariants

1. A child cannot resolve its own request.
2. Model-provided ids are lookup hints, never authority.
3. Resolver authority requires identity of the handler-injected `parent_agent`, active child object, and (for Desktop/TUI) the captured live transport plus live session-record generation.
4. Approval is `once` for one exact raw command byte string and one child tool call. No pattern/session/permanent allowlisting.
5. Hardline blocks, explicit deny rules, missing sudo credentials, privilege escalation, production/external effects, secret changes, security-control changes, and other owner-consequence classes never enter the parent-resolvable lane; they use the existing user fallback or remain unconditionally blocked.
6. Pending requests expire, are single-consumption, and are revoked on child completion, parent/session reset, transport rebind, interrupt, or process exit.
7. The child receives no approval id and no resolver operation/tool.
8. The model cannot set YOLO, alter `approvals.mode`, broaden scope, change the command, or choose `session`/`always`.
9. Raw command text is never written to routine logs; audit records carry a redacted display plus SHA-256 digest and identities.
10. Silence, stale capabilities, mismatches, and resolver errors fail closed.

## Proposed architecture

### 1. Bind delegated authority at spawn

Add an immutable internal `DelegatedApprovalAuthority` to each active subagent record:

- `owner_agent`: exact parent `AIAgent` object identity
- `child_agent`: exact child `AIAgent` object identity
- `subagent_id`, `child_session_id`, `parent_session_id`
- existing `owner_session_id`, `owner_transport`, `owner_session_record`
- `delegation_id` when backgrounded
- monotonic creation/expiry timestamps

Pass it through a new ContextVar in `agent/delegation_context.py` only while `child.run_conversation()` executes. Do not serialize it or place it in environment variables.

### 2. Intercept before the user gateway branch

After hardline/deny/sudo checks and scanner gathering, but before `_gateway_notify_cbs`, `tools.approval` checks for a valid delegated authority.

Eligible lane (all required):

- delegated ContextVar present and active registry identities still match
- local, non-production environment; no host privilege escalation
- scanner result is approvable (not an unconditional floor)
- classification says `specialist_local_reversible`
- parent policy/identity explicitly owns that class (D-CC-12 supplied by trusted profile policy, not model arguments)

Create `_DelegatedApprovalEntry` with a CSPRNG `approval_id`, exact raw-command digest, raw command retained only in memory, redacted display, scanner findings, exact child tool-call id, expiry (recommended 90 seconds), and authority identities. The child blocks on its private Event.

Ineligible requests call the existing user path unchanged.

### 3. Surface a typed parent event

Publish `type="delegated_approval_request"` on a process-local parent-event rail with:

- `approval_id`, `subagent_id`, `child_session_id`
- redacted command display and description
- digest, scanner severity/rules
- fixed choices: `once`, `deny`, `escalate_to_user`
- expiry timestamp
- an explicit marker that command/description are untrusted data

Delivery rules:

- Background delegation: reuse the async-delegation fresh-turn watcher and ownership filters, but do not persist a process-local capability across restart.
- Synchronous delegation: when a request appears, return a typed `approval_required` result from the existing `delegate_task` call and keep the now-paused child owned by the async registry. This avoids re-entering the parent model while its tool call is still executing.
- Never splice the event into an in-flight assistant/tool sequence and never mutate the system prompt.

The parent sees a system-authored typed block. Untrusted command text is JSON encoded and bounded. Prompt injection can still influence an LLM's choice, but the capability's exact-command/child/once-only binding prevents any injected text from widening authority.

### 4. Resolve through the existing `delegate_task` tool

Extend `delegate_task` with an optional discriminated `approval_response` object:

```json
{
  "approval_id": "opaque-id",
  "choice": "once|deny|escalate_to_user"
}
```

When present, spawning fields are forbidden and normal child creation is skipped. The registry resolves only if:

- handler-injected `parent_agent is entry.owner_agent`
- active child object and all child/session ids still match
- Desktop/TUI transport and live session-record identities still match
- request is unresolved and unexpired
- exact raw-command digest and tool-call id still match the blocked guard

`once` sets only that entry's Event. `deny` sets only that entry's Event. `escalate_to_user` atomically transfers that same entry to the existing user notify queue without minting a broader request. Any mismatch returns a non-oracular generic refusal and leaves the command blocked.

This adds no new core tool and exposes no resolver to a child because delegated leaves do not inherit `delegate_task`; an orchestrator child may have `delegate_task`, but its injected `parent_agent` identity is the orchestrator, not its own parent, so it cannot resolve its own entry.

### 5. Audit

Emit lifecycle hook records for request and terminal resolution:

- request id, digest, redacted display
- parent/child/subagent/delegation ids
- scanner findings and eligibility classification
- decision (`once`, `deny`, `escalated`, `expired`, `revoked`)
- `decided_by="parent_agent"|"user"|"timeout"`
- timestamps and reason code

Never log the capability object or raw command.

## Regression-first test plan

Implement as vertical slices, running each RED before production code:

1. Context isolation: delegated child receives an unforgeable parent authority but not a parent user-notify route.
2. Exact once: matching parent agent + child + request approves the exact blocked command once.
3. Command substitution: same approval id cannot approve a one-byte-different command.
4. Child self-approval: child-agent identity is rejected even with the id.
5. Sibling theft: another child and another parent session are rejected.
6. Replay: a consumed id resolves exactly once.
7. Expiry/revocation: timeout, child finish, interrupt, reset, transport rebind, and session-record replacement all deny/revoke.
8. Scope: `session`, `always`, model-set YOLO/config fields, and spawning fields combined with `approval_response` are schema/runtime rejected.
9. Owner fallback: irreversible/external/privileged/security/secret classifications still emit the existing Desktop user card.
10. Smart-DENY behavior: parent may only select exact `once`, never persistence.
11. Async delivery: request arrives as a fresh owned internal turn with legal role alternation and no prompt rebuild.
12. Sync conversion: synchronous delegation safely returns `approval_required`, pauses rather than abandons the child, and resumes to one final result.
13. Batch: concurrent requests remain child/request keyed; no FIFO-by-parent ambiguity.
14. Audit/redaction: hooks contain identities/digest and no raw secret.
15. End-to-end Desktop/TUI: parent resolves local reversible request without a Desktop card; `escalate_to_user` produces the existing card and response path.

## Why production code is deferred

A correct implementation must atomically coordinate four existing lifecycles: blocking approval, live child ownership, synchronous-to-paused delegation conversion, and cache-safe parent wake delivery. Implementing only the registry or only the Desktop RPC would create a deadlock/orphan/self-approval risk. The source has all required seams, but this is not a small one-file safety patch.

## Rollback

No live activation is proposed. For a future implementation, rollback is removal of the resolver branch; pending in-memory entries fail closed on process exit and no allowlist/config migration exists.
