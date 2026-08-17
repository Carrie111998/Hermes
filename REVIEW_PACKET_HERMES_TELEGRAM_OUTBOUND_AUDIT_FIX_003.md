# Review Packet: HERMES-TELEGRAM-OUTBOUND-AUDIT-FIX-003

## Review boundary

- Repository: `cwliao/hermes-agent`
- Base: verified `origin/main` at `4823f1e3228f9e4e90e295924fc2d609cbc3d5a7`
- Scope: preserve Telegram event correlation metadata through normal gateway
  progress/status paths when a Telegram DM has no thread ID.
- Out of scope: Telegram configuration, credentials, message content, user or
  chat identity, webhook state, Relay enablement, DGX mutation, and delivery
  claims.
- Reviewer instruction: review this packet only; do not edit the worktree.

## Observed failure

The Telegram adapter emits metadata-only outbound audit records only when an
opaque delivery correlation ID reaches the adapter. In a normal Telegram DM,
the source has no thread ID. `gateway/run.py` previously constructed
`_progress_metadata` and `_status_thread_metadata` only when a thread ID was
present, so event-scoped correlation metadata was discarded before progress or
stream sends. The send could still succeed and be visible, while the outbound
audit record was intentionally absent.

## Proposed correction

When event metadata is present, build both metadata objects through the existing
`_thread_metadata_for_event_data` helper even when no progress thread exists.
The helper adds only the opaque Telegram correlation field in the DM case; it
does not turn the inbound event message ID into a Telegram thread or reply
target.

## Invariants

1. Telegram DM event correlation survives normal progress/status/stream paths.
2. Telegram DM event message IDs are not reused as thread metadata.
3. Existing topic/thread routing remains unchanged.
4. No message body, token, credential, raw identity, or absolute sensitive path
   enters the audit metadata.
5. Empty or unrelated event metadata preserves the prior `None` behavior when
   no thread routing exists.

## Changed-file manifest

- `gateway/run.py`
  - Preserve event metadata for progress and status/stream metadata construction
    when no thread ID exists.
- `tests/gateway/test_run_progress_topics.py`
  - Extend the Telegram DM regression to require correlation preservation while
    asserting no thread routing is introduced.

## Verification evidence

- Targeted gateway suite: `77 passed`.
- New Telegram DM regression: `1 passed`.
- Ruff on changed files: PASS.
- Python compile check on changed files: PASS.
- `git diff --check`: PASS.
- Full `tests/gateway` run: not completed within the bounded 180-second local
  window; no failure result is claimed from that timeout.

## Review questions

1. Does the correction preserve the correlation invariant for normal Telegram
   DM progress/status/stream paths without changing routing semantics?
2. Does the regression test cover the observed missing-outbound-audit path and
   prevent reuse of the event message ID as a thread?
3. Is the change limited enough to avoid unrelated platform behavior changes?
4. Are the metadata and evidence boundaries safe?
5. Is any additional correction required before merge or deployment?

## Gate status

This packet requests implementation review only. It does not claim commit,
push, merge, CI, DGX deployment, runtime health, inbound polling, outbound
delivery, or Telegram user-visible delivery.
