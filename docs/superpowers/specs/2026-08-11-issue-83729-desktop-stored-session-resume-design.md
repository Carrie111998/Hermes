# Issue 83729 Desktop Stored Session Resume Design

## Problem

Opening a persisted Desktop session can leave the thread blank even though the
REST transcript is available. The cold resume path starts the transcript fetch
and `session.resume` concurrently, but it does not publish the transcript until
the RPC settles. A delayed or stranded RPC therefore holds usable persisted
history behind the loading state.

The empty-transcript failure check also relies only on the cached sidebar row's
`message_count`. If that renderer cache is missing or stale, a successful resume
response can report persisted history while an empty transcript is accepted as
a valid session view.

The durable stored session is not invalid in this failure. Runtime session IDs
are process-local bindings and may be replaced across backend restarts. The fix
must keep those identities separate and must not change the existing stale-route
404 behavior.

## Goals

- Paint a valid REST transcript as soon as it arrives, independently of the
  `session.resume` RPC settling.
- Never let an older resume attempt overwrite the currently selected session.
- Preserve pending local messages when reattaching the same session.
- Avoid rebuilding a large transcript when the later RPC adds no live tail.
- Enter the existing bounded retry and explicit error state when authoritative
  metadata says history exists but no transcript can be painted.

## Non-Goals

- Changing the behavior for a stored session that genuinely returns 404.
- Adding a new timeout policy to REST or gateway requests.
- Replacing the existing resume hook with a new state machine.
- Changing watch-window lazy resume behavior.

## Chosen Design

The REST prefetch remains concurrent with `session.resume`, but its completion
becomes an independent publication point. When it resolves, the resume attempt
must still match both the current request generation and selected stored session.
The hook then reconciles the persisted messages with the current view, preserves
same-session pending turns and local assistant errors, and publishes only when
the resulting array differs.

The same reconciled array is retained as the cold resume's local snapshot. When
the RPC later settles without an inflight or queued projection, final resume
reconciliation reuses that array and does not publish the transcript again. If
the RPC carries a live projection, the hook grafts only that tail onto the REST
base and publishes the changed result.

The stale-result guards remain authoritative. A prefetch belonging to a resume
attempt that is no longer current is ignored; it does not arm a failure latch or
paint another session's transcript.

## Failure Semantics

After `session.resume` settles, the hook treats the session as expected to have
history when either:

- the resolved sidebar/session row has `message_count > 0`, or
- the authoritative resume response has `message_count > 0`.

If either source says history exists and the pre-recovery transcript is empty,
the hook clears the runtime binding and arms `$resumeFailedSessionId`. The
existing bounded retry flow then retries and ultimately presents the existing
inline error with a manual Retry action.

A failed REST prefetch remains non-fatal when the RPC supplies a usable
transcript or accurately reports an empty session. Existing RPC-failure REST
fallback and genuine-404 behavior remain unchanged.

## Testing

Hook-level tests will prove these behavior contracts:

1. A resolved REST transcript is visible while `session.resume` remains pending.
2. A stale REST completion cannot overwrite a newer selected session.
3. A missing sidebar row cannot suppress the failure latch when the resume
   response reports `message_count > 0` but no messages are available.
4. A later RPC response with no live projection retains the already-published
   transcript rather than producing a different message array.

The focused hook suite, Desktop TypeScript typecheck, and relevant lint checks
must pass before completion.
