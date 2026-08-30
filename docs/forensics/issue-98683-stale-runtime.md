# Forensic report: #98683

## Verdict

**Confirmed.** The desktop symptom is a real stale-runtime polling bug. A session can remain bound to a reaped runtime ID; status polling then receives terminal gateway error `4001` (`session not found`) indefinitely. The repeated status-stack remount path clears the terminal polling latch, so the UI retries instead of converging.

## Root cause, line by line

At the affected pre-fix path:

1. `apps/desktop/src/app/chat/composer/status-stack/index.tsx` mounted an effect for the current `sessionId`.
2. That effect called `resetBackgroundPollingGuard(sessionId)` on every mount/remount.
3. `apps/desktop/src/store/composer-status.ts` receives `process.list` for the stale runtime and recognizes gateway code `4001` as terminal.
4. The store records the runtime in `goneSessions` and invokes `markRuntimeGone(sessionId)`.
5. React/UI state changes cause the status stack to remount.
6. The mount effect clears `goneSessions` again before the next scheduled poll.
7. The next `process.list` request hits the same dead runtime and returns `4001` again.
8. Repetition causes the reported blinking UI, focus loss, and clarify polls that never become answerable.

The surgical fix removes only the remount-time reset. Explicit reset behavior remains available for an actual runtime reconnect, while terminal recovery unbinds the dead runtime, resumes from the stored session ID when applicable, and caps repeated healing attempts.

## Relevant implementation nodes

- `apps/desktop/src/app/chat/composer/status-stack/index.tsx`: session status-stack mount effect; no longer clears the dead-runtime latch during remount.
- `apps/desktop/src/store/composer-status.ts`: background `process.list` poll, `goneSessions` terminal latch, `4001` classification, and alive-state reconciliation.
- `apps/desktop/src/store/runtime-gone.ts`: idempotent stale-runtime recovery; tile unbinding, primary-session resume, retry cap, and healthy-runtime budget reset.
- `apps/desktop/src/store/session-states.ts`: runtime/tile binding state changed by recovery.
- `apps/desktop/src/store/session.ts`: stored-session resume path.

## Graphify architecture map

From `graphify-out/GRAPH_REPORT.md` (generated report; it records its own need to be refreshed with `graphify update .`):

- `SessionDB` community: **15**, **272 nodes** (report lines 5616–5618).
- `conversation_compression.py` community: **158**, **200 nodes** (lines 6188–6190).
- `conversation_loop.py` community: **183**, **228 nodes** (lines 6288–6290).
- `CompressionCommitFence` community: **206**, **66 nodes** (lines 6380–6382).

The direct #98683 path is in the desktop store/UI nodes above. The compression and lease nodes are adjacent safety contracts, not the direct cause of the desktop poll loop:

- `agent/conversation_compression.py`: `run_compress_context_with_progress_timeout` uses a cancellation boundary before commit and a `CompressionCommitFence` around post-summary mutation; cancellation before commit aborts, while an already-started commit completes safely.
- `gateway/turn_lease.py`: `SessionTurnLeaseRegistry.acquire` uses bounded lock acquisition and fails closed on timeout; a timeout waiting for a lock does not release a lock held by another owner.
- `agent/conversation_loop.py`: conversation execution/reset path.
- `hermes_state.py`: `SessionDB` write path uses transactional `BEGIN IMMEDIATE` writes with bounded retry behavior.

## Deterministic reproduction

Temporary repro: `C:/Users/Nitro/AppData/Local/Temp/repro_98683_stale_runtime.py`

Output:

```text
remount-reset requests=4
latched requests=1
lease timed_out=True lock_still_held=True
compression before_commit=aborted-before-commit after_commit_start=commit-completes-after-cancellation
```

Interpretation: clearing the latch on four remounts produces four repeated polls; retaining it produces one. The lease result demonstrates bounded acquisition timeout without stealing the held lease. The compression result demonstrates cancellation before commit and safe completion after commit begins.

## Duplicate / related issue audit

- #97158 — **closed**, not merged; cache affinity-key fix. Unrelated to stale desktop runtime polling.
- #97709 — **closed**, not merged; salvage of #97158. Unrelated to stale desktop runtime polling.
- #98554 — **open**, related facet: approval/goal polls against reaped runtime `4001` and composer focus loss.
- #98434 — **open**, closely related facet: boot-restored chat bound to a dead runtime; its report identifies remount reset as the mechanism that defeats latching.

This change addresses the umbrella behavior in #98683 rather than duplicating the cache-affinity work in #97158/#97709.

## Validation and security review

Focused regression coverage is in `apps/desktop/src/store/runtime-gone.test.ts`, including remount-safe latching, tile unbinding, primary resume, idempotency, retry cap, healthy-runtime recovery, terminal `4001` routing, and transient-error binding preservation.

The patch is local to desktop runtime state and does not add network endpoints, dynamic code execution, credentials, or persistence schema changes. Security assurance still requires the repository’s normal CI and review; no live production desktop backend was available for this investigation.
