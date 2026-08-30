# Forensic report: #98683

## Verdict

**Confirmed.** The desktop symptom is a real stale-runtime polling bug. A session can remain bound to a reaped runtime ID; passive/background `process.list` polling then receives terminal gateway error `4001` (`session not found`). Without runtime-keyed convergence and bounded session healing, later poll/remount paths can keep targeting the dead binding. The generic remount reset is intentionally retained for #98455 and is not part of this change.

## Root cause, line by line

At the affected pre-fix path:

1. `apps/desktop/src/store/composer-status.ts` polls `process.list` for the bound runtime/session.
2. A reaped runtime returns terminal gateway code `4001` (`session not found`).
3. The previous recovery tracked a session-level gone condition without coupling convergence to the runtime identity and without healing the bound tile/session state.
4. The dead binding therefore remained eligible for later polling/re-render paths; approval/goal/clarify surfaces could continue targeting the reaped runtime.
5. The fix latches `4001` per runtime, unbinds the stale tile, resumes only the primary bound session from the stored session ID, and caps repeated healing attempts.
6. A healthy `process.list` clears the runtime latch and healing budget; transient failures preserve the binding.
7. Repeated `4001` events for the same runtime become no-ops, preventing the poll/remount/focus-loss loop while preserving intentional reconnect resets.

The generic remount reset in `status-stack/index.tsx` is intentionally retained for #98455 and is not part of this change.

## Relevant implementation nodes

- `apps/desktop/src/app/chat/composer/status-stack/index.tsx`: session status-stack mount effect; generic reconnect reset remains intact and is excluded because #98455 owns that behavior.
- `apps/desktop/src/store/composer-status.ts`: background `process.list` poll, `4001` classification, and alive/terminal routing.
- `apps/desktop/src/store/runtime-gone.ts`: runtime-keyed stale-runtime latch; tile unbinding, primary-session resume, retry cap, and healthy-runtime budget reset.
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
- `gateway/turn_lease.py`: `SessionTurnLeaseRegistry.acquire` uses bounded lock acquisition and fails closed on timeout; timeout waiting for a lock does not release a lock held by another owner.
- `agent/conversation_loop.py`: conversation execution/reset path.
- `hermes_state.py`: `SessionDB` write path uses transactional `BEGIN IMMEDIATE` writes with bounded retry behavior.

## Deterministic reproduction

Temporary repro: `C:/Users/Nitro/AppData/Local/Temp/repro_98683_stale_runtime.py`

Expected output:

```text
remount-reset requests=4
latched requests=1
lease timed_out=True lock_still_held=True
compression before_commit=aborted-before-commit after_commit_start=commit-completes-after-cancellation
```

Comparison fixture: clearing a latch on four remounts produces four repeated polls; retaining it produces one. This demonstrates the generic latch failure mode, while this implementation handles runtime-keyed terminal recovery and bounded healing. The lease/compression lines are adjacent contract checks, not claims that this desktop patch changes compression or lease semantics.

## Duplicate / related issue audit

- #97158: CLOSED (`2026-08-30T13:26:07Z`), not merged; cache affinity-key fix, unrelated.
- #97709: CLOSED (`2026-08-30T13:26:21Z`), not merged; salvage of #97158, unrelated.
- #98554: OPEN; approval/goal-status polls never latch off a reaped runtime (`4001` loop), materially overlapping.
- #98568: OPEN; cron-related branch for #98554, materially overlapping.
- #98629: OPEN; heartbeat identity/re-render flicker and approval replay, adjacent.
- #97887: OPEN; recovery of visible sessions after stale-runtime RPC, adjacent.
- #98434: OPEN; boot-restored stale runtime, adjacent.
- #98455: OPEN; status-stack remount reset behavior, exact overlapping mechanism; intentionally retained here and excluded from this PR.
- #98681: OPEN; hidden panes flashing composer focus, adjacent.
- #98702: OPEN; persisted live clarification cards, adjacent.
- #94572: OPEN; session-scoped UI actions after runtime remint, adjacent.
- #41678: OPEN; desktop compression session lineage, adjacent.
- #94950: CLOSED; prior status-stack polling of a dead session.
- #95495: MERGED; broad polling safeguards.

Conclusion: #97158/#97709 are not duplicates. The focused change here is the runtime-keyed stale-runtime healing path; it does not duplicate #98455's generic remount reset.

## Luna review

- Correctness: one-shot handling per runtime; idempotent repeated terminal events; bounded healing per stored session.
- Concurrency: maps/sets are process-local; the runtime is latched before side effects; repeated/reentrant callbacks cannot multiply recovery.
- Safety: recovery uses store-derived IDs only; no external input, secrets, persistence schema, or unbounded retry.
- Regression: tile-only stale views are unbound without navigation; only the primary bound session is resumed; orphaned bindings have no side effect; transient failures preserve the binding.
- Security assurance: no new endpoint, credential flow, code execution, or filesystem write.
- Validation scope: focused runtime-gone test passed; broad validation intentionally skipped per request.

## Recommendation

Merge the surgical runtime-keyed latch, stale-tile unbinding, primary-session resume, and bounded healing budget. Keep generic reconnect/remount reset behavior owned by #98455.
