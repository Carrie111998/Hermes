---
title: "HERMES-TELEGRAM-DELIVERY-VERIFICATION-001: prove one user-visible Telegram response"
status: MERGED_DEPLOYED_RUNTIME_HEALTH_PASS_DELIVERY_UNPROVEN
date: 2026-08-17
type: verification
ticket: HERMES-TELEGRAM-DELIVERY-VERIFICATION-001
target_repo: hermes-agent
---

# HERMES-TELEGRAM-DELIVERY-VERIFICATION-001: prove one user-visible Telegram response

## Status and boundary

Current gate: `MERGED_DEPLOYED_RUNTIME_HEALTH_PASS_DELIVERY_UNPROVEN`.

This design review evaluates whether incomplete evidence is classified safely;
it does not require the current candidate to satisfy every delivery class.
The current candidate is intentionally `PARTIAL`, and no additional runtime
observation is authorized merely to make the design review pass.

This is a repo-local verification ticket. Its implementation scope is limited
to redacted, metadata-only Telegram observability needed for correlation. It
does not authorize Telegram configuration changes, DGX restart or deployment,
Relay enablement, credential changes, allowlist changes, webhook changes, or
any other runtime mutation. The ticket must keep ticket design, implementation,
tests, independent review, reconciliation, execution authorization, runtime
health, inbound polling, outbound delivery, and user-visible delivery as
separate gates.

The current deployed Relay release remains disabled by default. No Relay
behavior is part of this ticket.

## Context

The merged Hermes runtime has separate evidence for service health and inbound
polling progress. Those signals do not prove that a real Telegram user saw a
response. The next roadmap gate is therefore a single approved, bounded,
user-visible response/delivery verification using metadata-only evidence.

An operator-supplied Telegram screenshot is available as candidate direct UI
evidence. It shows an inbound user message at `19:11` and a bot response at
`19:12` in the same conversation. The screenshot itself, its path, message
bodies, and user identity are not stored in this ticket or sent to reviewers.
The candidate evidence remains `UNRECONCILED` until the design packet is
reviewed and the evidence boundary is accepted.

## Objective

Prove one approved Telegram response path end to end at the user-visible
boundary, while recording only the minimum metadata needed to correlate:

1. the approved test attempt;
2. the inbound event, if the runtime exposes a qualifying metadata record;
3. Hermes response/outbound delivery status; and
4. direct user-side visibility confirmation.

Each evidence class has its own result. Direct user confirmation may close the
`USER_VISIBLE_DELIVERY` sub-gate even when runtime telemetry is unavailable;
it must not be described as `INBOUND_POLLING` or `OUTBOUND_DELIVERY` evidence.
The overall ticket result is `PASS` only when the approved runtime path also
provides the required inbound and outbound metadata. A visible response with a
missing runtime class is `PARTIAL`; no direct user confirmation is
`UNPROVEN`.

The aggregation rule is closed:

- Overall `PASS`: all four class results are `PASS`.
- Overall `PARTIAL`: no hard safety/authorization block exists, direct
  user-visible confirmation is `PASS`, and at least one other class is
  `UNPROVEN` or otherwise incomplete.
- Overall `UNPROVEN`: no direct user-visible confirmation is available and no
  hard safety/authorization block exists.
- Overall `BLOCKED`: authorization, reviewer identity, privacy boundary,
  evidence safety, or stop-condition handling fails. Missing telemetry alone is
  not `BLOCKED`; it is `PARTIAL` or `UNPROVEN` according to the rules above.

For this ticket, the class contracts are:

- `SERVICE_HEALTH=PASS` only when bounded read-only service metadata reports
  the expected active/running process state. This is supporting context and
  never a delivery result.
- `INBOUND_POLLING=PASS` only when a qualifying accepted update or equivalent
  direct inbound event is correlated to the attempt. Empty polling progress is
  not sufficient.
- `OUTBOUND_DELIVERY=PASS` only when a send-success record from the approved
  runtime adapter is correlated to the attempt. User-visible confirmation is
  not a substitute for this runtime record.
- `USER_VISIBLE_DELIVERY=PASS` only when the approved user directly confirms
  that the response is visible in the Telegram conversation.

## In scope

1. Review the proposed approved test path and its safety boundary.
2. Reconcile exactly one metadata-only review packet with one authenticated
   Claude reviewer and one authenticated AGY reviewer.
3. Use only an explicitly authorized existing Telegram test conversation; do
   not create a new bot, recipient, webhook, allowlist entry, credential, or
   scheduled task.
4. Correlate the test attempt using bounded metadata only:
   - opaque correlation ID;
   - direction (`inbound` or `outbound`);
   - event/outcome class;
   - bounded local/UTC timestamps and latency;
   - safe platform message/update identifiers, only if already exposed by the
     approved runtime evidence; and
   - the result of direct user-visible confirmation.
5. Record the evidence class for each observation separately:
   `SERVICE_HEALTH`, `INBOUND_POLLING`, `OUTBOUND_DELIVERY`, or
   `USER_VISIBLE_DELIVERY`.
6. Treat the evidence classes as separate non-promoting gates: they may share
   the same real-world attempt, but no class may be inferred from another
   class's observation. The source of each class must remain explicit.
7. Preserve the raw screenshot outside the repository boundary and do not
   include it in review packets, commits, or generated artifacts.

## Out of scope

- Any source-code or configuration implementation.
- Any Telegram API call made only to manufacture evidence.
- Sending a new message, changing a message, deleting a message, or changing
  Telegram state without a separate explicit execution authorization.
- Restarting, redeploying, enabling Relay, changing credentials, changing an
  allowlist, setting a webhook, or modifying unrelated DGX services.
- Treating `systemctl` health, process liveness, polling progress, empty
  `getUpdates`, `getMe`, `getWebhookInfo`, CI, or a local synthetic test as
  user-visible delivery evidence.
- Recording message bodies, prompts, tokens, credentials, chat/user names,
  raw chat identifiers, absolute sensitive paths, or screenshots in the
  repository or reviewer packet.

## Approved test-path contract

The execution path is not authorized by this draft alone. Before execution,
the operator must explicitly approve all of the following:

- the already configured bot and existing test conversation;
- the single inbound action and single expected response;
- the bounded observation window and stop condition; and
- the metadata fields that may be retained.

The bounded observation and correlation window is at most five minutes from
the single approved operator action. Events outside that window cannot be
correlated to the attempt. If a required timestamp or correlation key is
missing, the relevant class remains `UNPROVEN` and the process stops.

This ticket has no outbound execution permission. The no-duplicate boundary is
therefore enforced by the gate itself: no new send or retry may be issued
under this ticket, and any request for another attempt requires a new explicit
authorization and a new review decision. The test must stop after one
correlated attempt, a clear success, or a clear
failure. It must not retry, send a second probe, alter Telegram state, or
continue after an ambiguous correlation. A user-side screenshot or equivalent
direct confirmation may establish `USER_VISIBLE_DELIVERY=PASS`; it does not
automatically establish the other evidence classes. The approved path must
use one operator action and one expected response, with no automated retry,
duplicate send, or second probe. If the attempt fails or correlation becomes
ambiguous, stop and record the failure; do not retry to manufacture evidence.

## Evidence contract

The metadata-only evidence record may contain only:

```text
ticket=HERMES-TELEGRAM-DELIVERY-VERIFICATION-001
correlation_id=<opaque bounded value>
evidence_class=<SERVICE_HEALTH|INBOUND_POLLING|OUTBOUND_DELIVERY|USER_VISIBLE_DELIVERY>
direction=<inbound|outbound|user_confirmation>
event_class=<bounded non-secret code>
observed_at=<bounded timestamp>
latency_ms=<non-negative integer when available>
platform_id_present=<true|false>
outcome=<PASS|PARTIAL|UNPROVEN|FAIL>
artifact_retained=<false>
```

No raw message content, screenshot bytes, user identity, chat identity,
token, credential, prompt, log line, absolute path, or free-form error text
may enter the packet. Platform identifiers must be omitted or reduced to a
safe presence/metadata marker unless the reviewer-approved packet explicitly
permits the identifier for correlation.

## Acceptance gates

### Design and review

- The ticket remains `DESIGN_REVIEW_PENDING` until exactly one authenticated
  Claude reviewer and exactly one authenticated AGY reviewer independently
  review the same metadata-only packet.
- The design verdict assesses the safety and completeness of the ticket
  contract, not whether the current candidate already has every runtime
  evidence class. Missing candidate classes must remain `UNPROVEN` or
  `PARTIAL` at this gate.
- The packet contains the ticket key, scope, invariants, evidence classes,
  changed-file manifest, and candidate evidence status only.
- Review verdicts are `PASS`, `REVISE`, or `BLOCKED`. A reviewer/session
  failure is `BLOCKED`, not a substitute approval.
- Any `REVISE` result produces one correction set; the exact corrected packet
  is resubmitted to both reviewer families and older verdicts are not carried
  forward.
- Reconciliation records whether both reviewers assessed the same packet and
  whether the ticket is safe to execute.

### Execution and evidence

- Separate explicit authorization exists for the approved Telegram test path.
- One bounded attempt is correlated across available inbound, outbound, and
  user-visible metadata without exposing content or identity.
- `USER_VISIBLE_DELIVERY=PASS` requires direct user-side confirmation that the
  response was visible in the Telegram conversation.
- `OUTBOUND_DELIVERY=PASS` requires an independent send-success record from
  the approved runtime path; user confirmation alone cannot replace it.
- `INBOUND_POLLING=PASS` requires a qualifying accepted update or equivalent
  direct inbound metadata; empty polling progress is supporting evidence only.
- If user-visible confirmation passes but inbound or outbound runtime metadata
  is missing or ambiguous, record the user-visible class as `PASS` and the
  overall ticket as `PARTIAL`. If user-visible confirmation is absent, the
  overall ticket is `UNPROVEN`; if authorization or evidence safety fails, the
  ticket is `BLOCKED`. Never upgrade a missing class from another layer.
- No source, configuration, Telegram state, DGX runtime, or persistent user
  data is modified by the verification.

## Gate order

Ticket draft -> identical metadata-only Claude/AGY design review ->
reconciliation -> explicit execution authorization -> one bounded approved
attempt -> metadata-only evidence capture -> independent evidence
classification -> final ticket update.

Commit/push, CI, merge, deployment, runtime health, inbound polling,
outbound delivery, and user-visible delivery remain separately reported. This
verification ticket has no implementation or deployment gate unless a later,
separately authorized ticket is opened.

## Review questions

1. Does the proposed evidence contract prove user-visible delivery without
   exposing message content or user identity?
2. Are the four evidence classes sufficiently separate and non-promoting to
   prevent service health or polling progress from being misreported as
   delivery?
3. Does the approved test-path contract prevent duplicate sends, retries, and
   unapproved Telegram state changes?
4. Is the screenshot-derived candidate evidence correctly classified as
   direct user-visible evidence while remaining outside the reviewer packet?
5. Are the `PARTIAL` and `UNPROVEN` outcomes explicit enough to prevent a
   missing runtime correlation from being silently upgraded?

## Current candidate evidence

- `SERVICE_HEALTH`: `PASS` as previously verified supporting context; it is not
  part of the user-visible delivery claim.
- `INBOUND_POLLING`: `UNPROVEN`; no qualifying runtime correlation is recorded
  for this candidate.
- `OUTBOUND_DELIVERY`: `UNPROVEN`; no correlated runtime send-success record is
  recorded for this candidate.
- Candidate class: `USER_VISIBLE_DELIVERY`.
- Source: operator-supplied screenshot, retained outside the repository.
- Visible timing metadata: inbound at `19:11`; response at `19:12`.
- Candidate class result: `USER_VISIBLE_DELIVERY=PASS`.
- Overall candidate result: `PARTIAL` until the required runtime inbound and
  outbound metadata correlation is independently established.
- Design-review result is separate from the candidate delivery result; no
  additional Telegram action is authorized to upgrade the candidate here.

## Final design-review reconciliation

The design-review packet was identical for both reviewer families and
contained metadata only. Claude and AGY both returned `PASS` after the closed
aggregation rule and five-minute correlation window were added. The review
verdict approves the ticket design only; the current candidate remains
`PARTIAL`, and no execution or Telegram mutation is authorized by this
reconciliation.

- Packet SHA-256:
  `1DF0BA6BA6366749EBDECE861408F17A3DEE56FF7485F47354C6DD9E6775DBCA`.
  No packet content or raw artifact is stored here.
- Authenticated Claude Code reviewer: version `2.1.233`, verdict `PASS`.
- Authenticated AGY reviewer: version `1.1.13`, verdict `PASS`.
- Consensus: `DESIGN_REVIEW_PASS` on the identical packet; no correction set
  remains.
- Raw artifact, message text, user identity, and screenshot path: withheld.
- Runtime metadata correlation: not yet established in the bounded read-only
  log query; do not claim `INBOUND_POLLING=PASS` or `OUTBOUND_DELIVERY=PASS`
  from the screenshot alone.

## Authorized execution attempt

- Authorization: explicit one-attempt Telegram test authorization was received
  before observation began.
- Bound: one existing conversation, one five-minute observation window, no
  retry, no second probe, and no Telegram or DGX mutation by the verifier.
- Read-only baseline: service was `active/running` with zero restarts at the
  start of the window. The effective unit reports `StandardOutput=journal` and
  `StandardError=journal`; the previously inspected gateway file was not the
  authoritative service output and its line-count result is discarded.
- Bounded result: the authoritative user-service journal returned five lines
  in the bounded window. No `accepted_update`, `message_id`, `delivery`,
  `success=true`, or `telegram_polling_progress` metadata was observed there.
  This absence means runtime correlation was not obtained; it does not prove
  that the operator sent no Telegram message.
- Read-only source diagnosis: the Telegram plugin's accepted text path builds
  and queues a `MessageEvent`; the explicit INFO log is emitted later by the
  batch flush path, and there is no stable accepted-inbound audit record. The
  outbound `send()` path returns `SendResult(success=True, message_id=...)`,
  but has no dedicated successful-delivery audit record. Therefore the
  requested metadata correlation is an observability gap in the current
  runtime, not evidence that the two user messages were absent or ignored.
  No implementation change is authorized by this ticket.
- Attempt classification: `SERVICE_HEALTH=PASS` supporting context;
  `INBOUND_POLLING=UNPROVEN`; `OUTBOUND_DELIVERY=UNPROVEN`; no new
  `USER_VISIBLE_DELIVERY` confirmation was established by this attempt;
  attempt overall `UNPROVEN`.
- Existing screenshot classification remains unchanged:
  `USER_VISIBLE_DELIVERY=PASS`, overall candidate `PARTIAL`.
- Stop condition: the five-minute window ended without correlation. No retry or
  second attempt is authorized by this record.

## Implementation under review

- `gateway/platforms/base.py` now preserves a Telegram event's opaque
  correlation id across normal background responses, active-session command
  responses, clarify responses, and error notifications.
- `plugins/platforms/telegram/adapter.py` assigns a per-event opaque id and
  emits metadata-only `delivery_audit` records for processable inbound events
  and correlated outbound text and native media send results. Records contain
  no body, token, credential, raw chat/user identity, or sensitive absolute
  path.
- `tests/gateway/test_telegram_delivery_audit.py` covers metadata propagation,
  successful correlated send evidence, inbound evidence, and no-correlation
  no-op behavior.
 - Targeted verification: 41 tests passed across the new audit tests,
   Telegram text batching, and Telegram final delivery; Ruff and `git diff
   --check` passed in both the Windows shared environment and the isolated
   WSL reviewer environment.
 - CI correction: the first PR run exposed a legacy regression fixture using a
   reduced event namespace without optional metadata fields. The audit hook now
   uses `getattr` for those optional fields; the local targeted/regression suite
   is `41 passed`.
- First implementation review correction: authenticated Claude returned
  `REVISE` because native media paths lacked outbound audit records; the
  correction added audit coverage for native file/media sends and tests.
  The prior packet is not a final review artifact.
 - Second implementation review: authenticated AGY returned `PASS`; Claude
   returned `REVISE` because voice, animation, and media-group paths lacked
   dedicated tests. Those three tests are now added, and the targeted suite is
   `35 passed`.
 - Third implementation review: authenticated Claude returned `REVISE`
   because the edit-based streaming/finalize path did not emit the required
   outbound audit record. The public `edit_message` path now audits its
   `SendResult`, and a correlated edit test was added; the targeted suite is
   `35 passed`.
 - Fourth implementation review: authenticated Claude returned `REVISE`
   after reproducing duplicate outbound audit records on native-media fallback
   and orphaned batch correlation ids. Fallback auditing and batch metadata
   merging were corrected, with dedicated tests; the targeted suite is now
   `38 passed`.
 - Final implementation review reconciliation (superseded by the CI
   correction): `PASS` from one authenticated Claude reviewer and one
   authenticated AGY reviewer, both reviewing the byte-identical packet
   `0F9A7B9C962BE656508926354EABB937EBFA308541D1D7A6B5AA11125E9C91F2`.
   Claude independently confirmed the fallback and batching traces and noted
   that its own environment could not rerun the suite; the same 38-test suite
   was reproduced in the isolated WSL environment and AGY also confirmed the
    38-test result. No reviewer edited the worktree.
- CI correction review reconciliation: `PASS` from one authenticated Claude
   reviewer and one authenticated AGY reviewer, both reviewing the identical
   corrected packet
   `7E72D5448F0D97585ADEC40AB0DA92FD40DDD2595DE61EE15B765F5930206487`.
   Windows and isolated WSL both reproduced `41 passed`; Claude's own session
   could not execute the suite because its environment lacked dependencies,
   but found no implementation or privacy defect. No reviewer edited the
   worktree.

## Merge, CI, and DGX deployment evidence

These are separate post-review gates. They do not close inbound polling,
outbound delivery, or user-visible Telegram delivery.

- Commit/push: implementation commits `19666a9030beab096d06eb620bf559b9f84fbf7b`
  and CI correction `57bfaf5d6b181864048b212f0aa23392609fdfb1` were pushed.
- Merge: PR #36 merged to `main` as
  `1aec269ff4adda5de67fa39b60f003e2faba4495`.
- CI: required checks passed in run `32039164808`, including all eight Python
  test slices, e2e, Ruff/ty, Windows footguns, Windows DGX wrappers, lock,
  contributors, common ancestor, and the aggregate required-check gate.
- DGX deployment: the release identity was
  `v2026.8.17-hermes-telegram-delivery-verification-1aec269f`, with marker
  `1aec269ff4adda5de67fa39b60f003e2faba4495` and effective drop-in
  `38-hermes-telegram-delivery-verification-1aec269f.conf`.
- Rollback: the prior Relay release and drop-in remain retained; a new
  rollback manifest and pre/post-deploy metadata were preserved for this
  release. Relay remains disabled.
- Restart/runtime: after the authorized restart, the service was
  `ActiveState=active`, `SubState=running`, MainPID `2372026`,
  `NRestarts=0`, `ExecMainStatus=0`; the process cwd matched the new release
  both immediately and after a bounded ten-second check.
- Cleanup: the temporary deployment clone was removed after verifying that
  the immutable release and marker were complete. The primary dirty checkout
  was preserved.

No Telegram configuration, credentials, allowlists, webhook state, or message
content was changed or recorded. No new inbound polling, outbound delivery, or
user-visible delivery claim is made by this deployment evidence.

## Safety stop conditions

Stop and record `BLOCKED` or `UNPROVEN` if:

- the reviewer is not a real authenticated Claude or AGY session;
- the two reviewers did not receive byte-identical metadata-only packets;
- the approved conversation or operator authorization is ambiguous;
- the runtime cannot provide a bounded correlation without exposing content;
- the response cannot be directly confirmed by the user;
- a second send, retry, configuration change, restart, or deployment would be
  needed to manufacture missing evidence; or
- any command would expose a token, credential, message body, user identity,
  raw chat identifier, or sensitive absolute path.

## Current status

`MERGED_DEPLOYED_RUNTIME_HEALTH_PASS_DELIVERY_UNPROVEN`. The screenshot remains
candidate evidence for the user-visible class. Runtime health, inbound
polling, outbound delivery, and user-visible delivery remain separate gates;
this ticket does not claim a new user-visible delivery or authorize a retry.
