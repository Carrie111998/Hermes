# Tracker Applier Convergence-Reaper — Design

**Date:** 2026-07-20
**Status:** Approved (design); implementation pending
**Author:** Diego + agent
**Touches:** the single-writer intent-applier path — treat with the same care as auto-re-drive.

## Problem

On 2026-07-18 the `TRACKER_PARTIAL_BACKLOG` alert fired repeatedly for ~3 days over
10 partial tracker intents (2 `approved`, 8 `archived`) that were **already
converged in both native Postgres and the tracker canonical `pipeline.json`**. A
human had to enumerate, verify, and clear them to `processed/`.

Root cause of the stall: a partial that reaches the re-drive attempt cap is
classified **"capped"** and deliberately left in
`~/.hermes/mailbox/tracker/partial/` for the alert. Nothing auto-clears a capped
partial — even after its Postgres row reaches (or passes) the intent's target
stage. The alert keeps firing until a human acts.

### Why the existing pre-flight didn't already handle it

The applier already has a convergence check: Fix A's
[`IntentApplier._already_satisfied`](../../../intent_applier/applier.py) reads
native Postgres `current_business_state` via `NativePgJobStateReader` and tests it
against the `_STAGE_SATISFIED_BY` map. When a partial re-drives back into `inbox/`,
step 2b runs that check and reaps a converged intent as `satisfied`.

But a **capped** partial is never re-driven — `redrive_partials()` skips it — so
the pre-flight never runs on it again. If Postgres converges *after* the cap (the
incident pattern: archives were stamped in PG at `07-18T00:02:18Z`, after the files
hit `.rd5`), the pre-flight has no path back to that file. It sits and alerts
forever.

### Findings that shape the design (verified against live code/data, not memory)

1. **`max_redrive_attempts` is dead code.** It is stored on the applier but never
   used in logic. The *only* thing that produces a "capped" classification is
   `redrive_give_up_attempts`, which **defaults to 0 ("never give up") and is not
   set in `profiles/main/.env`**. So in the current deployment "capped" cannot
   occur — the reaper only has a steady job once a finite cap is re-enabled. (The
   memory's "hit max_redrive_attempts=5 → capped" describes a prior code/config
   state.) The dead field should be removed as cleanup so it stops misleading
   readers.

2. **The convergence machinery mostly exists.** `_already_satisfied` + the
   `_STAGE_SATISFIED_BY` legacy-stage→business-state map + a live `psycopg`
   (3.3.3) reader already answer "is this job at/past target in PG?". The reaper
   reuses them.

3. **The canonical `pipeline.json` `.stage` field is *legacy* space, not
   business-state space.** For the incident approvals, `.stage`/`.business_state`/
   `.pipeline_stage` = `review` (legacy), while `.currentBusinessState`/
   `.control_plane_business_state` = `materials_ready` (business_state). Since
   `_STAGE_SATISFIED_BY` is valued in business_states, gate B **must** read a
   business-state field (`currentBusinessState`), not `.stage`. Reading `.stage`
   would fail-closed on the exact incident cases and silently never reap them.

4. **`pipeline.json` is 41 MB / 4662 jobs.** A full `json.load` is a ~1–2 s
   GIL-holding stall of the applier's 1 s poll loop. Acceptable only if rare.

## Goals

- Auto-clear a **capped** partial whose target stage is already converged, without
  a human in the loop.
- **Fail closed**: only reap when convergence is confidently confirmed. A wrong
  reap burns the idempotency key and strands a real transition (PG never updated)
  — strictly worse than leaving it to alert.
- **Never re-drive**: re-driving a past-stage intent regresses state (e.g. an
  `approved` intent re-applied over a job now at `materials_ready` would pull it
  back). The reaper only ever moves capped → `processed/`.
- **Read-only until converged**: no `:4100` POST, ever. The reaper reads PG and an
  on-disk file, then either moves a file or does nothing.
- Stay on the single-writer applier thread — no new race surface.

## Non-goals (YAGNI)

- Reaping **non-capped** partials. They already self-clear via the pre-flight
  during normal re-drive; touching them adds redundant 41 MB reads.
- Extending `_STAGE_SATISFIED_BY` to cover `applied`/`final_submission`/`scored`.
  Both incident stages (`approved`, `archived`) are covered; an uncovered stage
  simply won't reap (fail-closed) and still surfaces via the alert, exactly as
  today.
- A new event type or notification. The existing `TRACKER_PARTIAL_BACKLOG` alert
  re-arming when the count drops is the operator-visible signal.

## Design

### Operating model

Re-enable a **finite** re-drive cap and add the reaper as its safety valve:

- `TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS=5` in `profiles/main/.env`. With the
  pre-flight active, a converged partial clears during re-drive *without* a `:4100`
  POST; the cap only stops genuinely-lagging partials from hammering `:4100`
  forever (~1 hr of exponential backoff before capping).
- The reaper catches any partial that converges *after* the cap.

Together: pre-flight handles "converged before cap", reaper handles "converged
after cap", cap stops "never converges" from hammering `:4100`.

### The reap gate (both must pass; fail-closed on anything else)

For a capped partial's parsed intent with `requested_stage` and
`job_id` (== PG `external_job_key` == canonical `pipeline.json` dict key):

- **Gate A (PG):** `_already_satisfied(msg)` — native PG `current_business_state`
  ∈ `_STAGE_SATISFIED_BY[requested_stage]`. Reuses the existing method verbatim.
- **Gate B (canonical file):** `pipeline.json.jobs[job_id].currentBusinessState`
  ∈ `_STAGE_SATISFIED_BY[requested_stage]`. Same map, second independent snapshot
  (on disk, written by the tracker's own sync cycle). Requiring agreement catches
  the mid-sync window where PG and the tracker canonical disagree.

Any of: reader `None`/error, stage not in the map, `job_id` absent from
`pipeline.json`, file unreadable, business-state field missing → **not converged →
do not reap** (leave capped, keep alerting).

### Cost control — gate B is second

`pipeline.json` (41 MB) is parsed **at most once per sweep**, and **only** when at
least one capped partial has already passed gate A. No capped partials, or none
PG-converged → the big file is never opened. The parse result is reused across all
gate-A candidates in that sweep.

### The reap action

Mirrors the existing `satisfied` path exactly:

```
idempotency.mark_applied(key, message_id=...)   # burn key immediately
_move_to(partial_file, processed_dir)           # never inbox/
```

Burning the key immediately (not just on next restart via
`rehydrate_from_processed`) means no regression re-drive is possible even before a
restart.

### Placement & cadence

New method `IntentApplier.reap_converged_partials() -> dict[str, str]` (pure,
always-acts, like `redrive_partials`). Flag-gated wrapper
`TrackerIntentApplierSubscriber.reap_converged_partials()` guards it behind the new
feature flag. Called from the dedicated applier thread in
`events/gateway_integration.py`, in the same once/min block, immediately **after**
`redrive_partials()` — so it mops up exactly what re-drive just classified capped.

Returns `{filename: "reaped" | "not_converged" | "skipped"}`.

### Feature flag

`TRACKER_APPLIER_REAP_CONVERGED_ENABLED`, **default off**. Independent of
`TRACKER_APPLIER_REDRIVE_ENABLED` — the reaper never POSTs to `:4100`, so it does
not need the `:4100` idempotent-no-op hard gate. Default-off allows deliberate
enablement after a soak.

### Observability

Log-only, no new event type:

- One `INFO` per reap: `job_id`, `requested_stage`, PG `current_business_state`,
  canonical `currentBusinessState`, `"converged post-cap, auto-reaped"`.
- Reaped-count folded into the existing subscriber tick log.

## Components & interfaces

| Unit | Responsibility | Depends on |
|---|---|---|
| `IntentApplier.reap_converged_partials()` | Sweep capped partials; reap gate-A∧B-converged ones; move to `processed/`; burn key. Pure/always-acts. | `_already_satisfied`, `_STAGE_SATISFIED_BY`, a canonical-`pipeline.json` reader, `idempotency`, `_move_to` |
| Canonical pipeline reader (new small helper) | `job_id -> currentBusinessState \| None`, best-effort, parsed once per sweep. Fail-soft. | `profiles/tracker/workspace/pipeline.json` path |
| `TrackerIntentApplierSubscriber.reap_converged_partials()` | Flag gate + logging + count. | env flag, applier |
| `gateway_integration` applier thread | Call reaper after re-drive, once/min, single-writer. | subscriber |
| `profiles/main/.env` | `GIVE_UP_ATTEMPTS=5`, `REAP_CONVERGED_ENABLED` flag. | — |
| Cleanup | Remove dead `max_redrive_attempts`. | — |

## Testing (TDD)

Unit (pure, injectable clock/readers — no real mailbox, PG, or gateway):

- Capped + gate A pass + gate B pass → `reaped`; file in `processed/`; key burned.
- Capped + gate A pass + gate B *fail* (canonical still behind) → `not_converged`;
  file stays in `partial/`; key **not** burned.
- Capped + gate A fail (PG behind / reader `None`) → gate B never consulted (file
  not even parsed); `not_converged`.
- Non-capped partial → never touched by the reaper.
- `requested_stage` not in `_STAGE_SATISFIED_BY` → `not_converged`.
- `job_id` absent from `pipeline.json` → `not_converged`.
- Reaper never moves a file to `inbox/` (no regression re-drive).
- Gate B parses `pipeline.json` at most once per sweep, and zero times when no
  gate-A candidate exists.
- Incident regression fixture: the 2 `approved`-at-`materials_ready` and an
  `archived`-at-`archived` case reap; an `approved`-at-`scored` (genuinely behind)
  does not.

Integration: enable the flag in a temp `HERMES_ROOT`, seed a capped partial + a
fake PG reader + a fixture `pipeline.json`, run one sweep, assert the file moved
and the idempotency DB shows the key applied.

## Rollout

1. Ship code + tests (agent-src, local-only, never push). Flag default off.
2. Set `GIVE_UP_ATTEMPTS=5` and `REAP_CONVERGED_ENABLED=1` in `profiles/main/.env`.
3. Enable on the next **natural** gateway restart — do **not** auto-restart the
   gateway (report PID + time for the operator to action).
4. Soak: watch for `auto-reaped` logs and confirm `TRACKER_PARTIAL_BACKLOG`
   re-arms without manual intervention on the next capped-and-converged case.

## Risks

- **Wrong reap strands a transition.** Mitigated by the two-gate fail-closed design
  and immediate key-burn semantics matching the audited `satisfied` path.
- **41 MB parse stalls the poll loop.** Mitigated by second-gate structuring (parse
  only when a gate-A candidate exists, once per sweep).
- **Field drift in `pipeline.json`/contracts.** If `currentBusinessState` or the
  business-state vocabulary changes, gate B fails closed (won't reap) rather than
  mis-reaping — safe degradation to today's manual behavior. `_STAGE_SATISFIED_BY`
  is already flagged "keep in sync with jobflow-api".
