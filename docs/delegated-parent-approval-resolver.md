# Parent-agent resolver for delegated command approvals

Status: implemented on this branch, default OFF, and intentionally not activated. Experimental/non-live pending independent acceptance and a separate operator decision for configuration and restart.

## Problem and implemented result

Historically, a delegated child inherited the parent's approval routing ContextVars. A flagged child command could therefore enter the parent's user-facing approval queue, but the parent model had no request capability or resolver operation. The implementation now provides a narrow process-local lane in which an active top-level background child can pause on one eligible command, the exact commissioning parent can decide it in a fresh turn, and the child can resume once.

The implementation reuses existing seams:

- `delegate_tool._active_subagents` binds the child to its commissioning parent and, for Desktop/TUI, the live transport and session-record generation.
- the process notification queue and gateway/TUI watchers deliver a typed event as a fresh turn while the parent is idle;
- `delegate_task` carries the parent-only resolver operation, avoiding another core tool.

No runtime default contains a static command digest list. The command is created by the child first, then bound dynamically to a one-request capability using its exact SHA-256 digest and tool-call id.

## Security contract

1. Model-provided ids are lookup hints, not authority.
2. Resolution requires handler-injected `parent_agent` object identity plus the active child registry record. Desktop/TUI additionally requires the same live transport object and session-record generation captured at dispatch.
3. The capability is bound to one raw command held in memory, its exact digest, one tool-call id, one child, and one request id.
4. Allowed decisions are only `once`, `deny`, and `escalate_to_user`. There is no session/permanent approval and no config mutation.
5. Child, sibling, unrelated parent, and self resolution are denied with a generic non-oracular response.
6. Hardline blocks and ineligible dangerous-command classes continue through the existing behavior: unconditional block or the established user approval path.
7. Requests expire and are single-consumption. Completion, interrupt, parent/session reset, transport/session-generation replacement, and process exit revoke pending requests and unblock the child as denied.
8. The child schema never advertises `approval_response`, including orchestrator children that retain ordinary delegation.
9. Raw command text remains only in the blocking in-memory entry. Parent events and approval hooks receive bounded, secret-redacted display text plus digest and identities.
10. Publish errors, stale identity, mismatches, timeout, and internal resolver errors fail closed.

## Exact eligibility

The parent lane is considered only after the existing hardline, explicit-deny, sudo, dangerous-pattern, host-access, and Tirith checks have produced an approvable request. Every condition below is required:

- `approvals.delegated_parent.enabled` is exactly YAML boolean `true` in the trusted parent profile;
- the caller is a top-level parent (`_delegate_depth == 0`);
- the child was successfully dispatched in the background; forced synchronous fallback disables the lane;
- the in-process delegated authority is present and still matches the active registry record;
- for Desktop/TUI ownership, the captured transport and session-record objects still match the live generation;
- environment type is exactly `local` and the terminal backend reports no host access;
- command is non-empty and at most 8192 UTF-8 bytes;
- Tirith reports no findings;
- every structured dangerous-pattern key is the single low-ambiguity inline-interpreter `-e`/`-c` class, represented by the canonical key `script execution via -e/-c flag` or its exact compatibility alias.

No command text heuristic expands this class. Missing/unknown/mixed pattern keys, shell `-c`, remote/container/SSH execution, host access, Tirith findings, and all other consequence classes are ineligible and preserve the existing user/hardline route.

## Dynamic exact-command flow

1. Trusted parent-side dispatch reads the feature flag and captures metadata on each background child. The model cannot set this metadata.
2. Child execution binds a frozen `DelegatedApprovalAuthority` through a ContextVar only for the child run.
3. When an eligible command reaches `check_all_command_guards`, the resolver creates a CSPRNG request id and stores the raw command, exact SHA-256 digest, tool-call id, authority, and monotonic expiry in memory.
4. A bounded redacted `delegated_approval_request` event is queued for the parent. Command and description are explicitly marked untrusted data.
5. Gateway and TUI deliver the event through their existing fresh-turn paths. TUI persists the event as a typed timeline row, not a user approval card, and preserves user/assistant role alternation.
6. The parent responds through `delegate_task(approval_response={approval_id, choice})`. Spawn fields are forbidden in the same call.
7. Runtime revalidates exact parent identity, active child authority, live transport/session generation, expiry, command digest, and tool-call binding before atomically consuming the request.
8. `once` resumes that one guard. `deny` fails closed without prompting the user. `escalate_to_user` consumes the parent capability and enters the existing user approval path.

## Schema and cache behavior

The commissioning parent receives an `approval_response` branch on the existing `delegate_task` schema. Its runtime handler also rejects mixed spawn/resolver arguments. Before a child's first model call, child tool schemas are deep-copied and that branch plus its conditional schema constraint are removed. This happens before the child's prompt cache exists and does not mutate the parent schema or registry schema.

Notifications are injected only at turn boundaries through existing fresh-turn delivery. The implementation does not mutate prior context or the system prompt.

## Event, audit, and lifecycle data

The in-memory entry contains the raw command. Persistable/display surfaces contain only bounded secret-redacted command/description text, digest, request/tool/child/delegation identities, pattern keys, expiry, fixed choices, and decision/reason metadata. Capability objects are never serialized.

Pending requests are revoked by:

- child completion/unregistration;
- explicit child interrupt;
- parent approval-session reset;
- live Desktop/TUI transport or session-record replacement detected while waiting;
- process exit via `atexit`.

Restart does not restore pending capabilities.

## Configuration and activation hold

The shipped default is:

```yaml
approvals:
  delegated_parent:
    enabled: false
```

This document records implementation behavior; it is not an activation instruction. Do not enable the experimental lane until an independent reviewer accepts the exact committed tree and the operator separately approves a config change and any required service/session restart. No install, live config edit, service restart, activation, push, or PR is part of this branch closeout.

## Verification scope

Regression coverage includes exact once/replay, dynamic non-prelisted commands, classifier boundaries, parent/child/sibling identity denial, command/tool-call substitution, expiry and lifecycle revocation, concurrent keyed requests, resolver schema exclusivity, child schema hiding, redaction/audit, fresh TUI typed-turn delivery without an approval card, and gateway fresh-turn delivery.

The detailed RED→GREEN and suite evidence is recorded in `docs/parent-approval-resolver-progress.md`.

## Rollback

Before activation, rollback is simply reverting the feature commit; the default remains OFF and there is no config migration or static allowlist to unwind.

After a separately approved activation, first set `approvals.delegated_parent.enabled` back to `false`, then perform only the separately approved session/service restart needed for the running host to pick up the change. Pending entries are process-local and fail closed on reset/restart/exit. Reverting the implementation commit remains the code rollback.
