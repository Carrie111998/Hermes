# Phase 2 — Runtime Integration Boundary

**Status:** Phase 2 **implementation started** (Task 19 `57a1ed651`); architecture checkpoint Task 20 (Policy C)  
**Date:** 2026-07-19 (planning); **updated** 2026-07-20 (Task 19 + Task 20)  
**Depends on:** Phase 1 closed — Task 17.1 `8fea4daa0`; baseline Git-reproducible at Task 18.5 `04b11bc4d`  
**Audience:** Architect + Cursor implementer

This document defines what Phase 2 **may** and **may not** do. Task 20 accepts **Policy C** (immutable finalized-run seal + successor-based recovery). Neither the seal nor the Recovery/Successor Run protocol is **implemented** until their dedicated tasks ship.

---

## 0. Phase 1 baseline (closed — historical)

Phase 1 delivered a manual 11-record run-level chain ending at `run_final_closure_record`.  
Closed at Task 17.1 `8fea4daa0`.

- JSON run-records are source-of-truth (SoT); event log is audit-only.
- Final closure is terminal for the **manual run-record chain** (Task 17.1 **implemented behavior**).
- `record_run_final_closure` does not mutate `run_manifest` / `task_status` / `attempt_status`.
- Task 17.1 did **not** claim or implement a global hard lock; task/attempt APIs remain callable in code today.
- Idempotent replay with matching audit event but missing JSON SoT **fails closed** (`InvalidTransition`).

**Task 20 does not rewrite Phase 1 history.** It establishes a **future Phase 2 policy** (Policy C) to be enforced at canonical mutation boundaries in Task 22+.

---

## 1. Policy C — Immutable finalization + Recovery/Successor Run

**Accepted at Task 20 (architecture only; not yet enforced in code).**

### 1.1 Immutable finalization (finalized-run seal)

Once a run has a valid `run_final_closure_record`, the **original run** will be permanently sealed against **all normal HTR mutation**, including:

- manual, CLI, Cursor, and runtime automation callers;
- run-level lifecycle mutations; task/attempt mutations; artifact-manifest mutations;
- workspace bootstrap/mutation targeting an already-finalized run;
- direct lifecycle-event append or JSON SoT writes on that run.

**Read-only observation** (`hermes htr observe`) remains allowed.

Enforcement must live at **canonical shared mutation boundaries** (all public mutation APIs), not only in a bypassable runtime wrapper. Applies to runs finalized before or after enforcement ships, when the closure record is valid.

**Not implemented until Task 22.**

### 1.2 Recovery/Successor Run (not in-place recovery)

If a finalized run later requires remediation, the **original run is not reopened, unlocked, edited, or rewritten**. An explicitly approved future process may create a separate **Recovery/Successor Run** linked to the original.

```
final closure
→ original run becomes immutable (future Task 22)
→ read-only observation remains available
→ a problem may produce a recovery proposal (future)
→ explicit high-risk approval required (future)
→ separate linked Recovery/Successor Run may be created (Task 27+)
→ diagnosis, remediation, verification, closure in the successor
→ original run unchanged as historical evidence
```

**Do not describe recovery as:** reopening, unlocking, editing final closure, rolling back closure, or resuming mutation on the original run.

**Not implemented until Task 27** (separate Architect-approved architecture + schema task).

### 1.3 Prohibited bypasses (normal operations)

No ordinary mutation API may gain: `force=True`, `unlock=True`, env-var override, direct SoT/event editing, deleting/renaming closure records, temporary suppression of closure checks, or lower-level helper bypass.

Exceptional legal/security/data-governance correction of a finalized original run requires a **separate Architect-approved exceptional-data-governance design** — not normal recovery.

---

## 2. Write-path gate

**No Phase 2 lifecycle write or invoke path may be enabled before immutable finalized-run enforcement (Task 22) is implemented and verified.**

| Work | Allowed before Task 22? |
|------|-------------------------|
| Read-only observability (Task 19) | ✅ Done |
| Derived action plan (Task 21) | ✅ Yes (read-only) |
| Approval persistence (Task 24) | ❌ No (operational control; still no invoke) |
| Human-gated lifecycle invoke (Task 25) | ❌ No |
| Bounded repair / unattended automation | ❌ No |
| Recovery/Successor Run creation | ❌ No (Task 27+) |

Bounded self-healing of finalized-run problems requires the Recovery/Successor Run protocol; **never in-place repair of the original run**.

---

## 3. Integrity, plans, approvals

| Topic | Stance |
|-------|--------|
| **Fail closed** | Required default (Task 19 observe + future invoke) |
| **Silent auto-heal** | Forbidden |
| **Derived action plan** | Task 21: library/stdout JSON; non-authoritative; not persisted in run tree |
| **Observation digest** | Canonical **semantic projection** of snapshot (exclude `observed_at`, presentation-only fields); deterministic JSON |
| **Confidence** | Deterministic classes: `high` / `medium` / `low` / `indeterminate` + reason codes; integrity errors → non-actionable |
| **Persisted approval** | Authoritative execution-control data (Task 24); schema task required; not lifecycle events |
| **Repair proposal (initial)** | Derived stdout/library JSON only; finalized-run recovery proposal format deferred to Task 27 |

---

## 4. Runtime read/write permissions (current + future)

| Resource | Read | Write (today) | Write (future) |
|----------|------|---------------|----------------|
| Phase 1 JSON SoT | Yes (Task 19) | Manual APIs only | Via Task 25+ invoke after Task 22 |
| Event log | Yes | Lifecycle APIs only | Same |
| Finalized original run | Yes | **Phase 1: still callable** | **Task 22: sealed** |
| Recovery/Successor Run | N/A | N/A | Task 27+ protocol |

Runtime must not append events or write SoT directly. Writes only through allowlisted canonical lifecycle APIs after gates pass.

---

## 5. Execution lock, verification, ambiguous outcomes

Future execution lock/lease (Task 23): run-scoped; owner + purpose; stale-owner handling; fail closed; not daemon/DB/distributed lock. Current `htr/io.file_lock` is **insufficient alone** (unused by lifecycle APIs; Windows not equivalent).

**First human-gated invoke (Task 25) must include:** pre-observe, plan + approval validation, lock, re-observe, stale rejection, one allowlisted API, **mandatory post-observe verification**, ambiguous-outcome handling, fail-stop (no blind retry). Verification cannot be deferred to a later task for the first write path.

Ambiguous outcomes include: not started; completed and verified; failed before mutation; may-have-completed (lost ack); SoT/event disagree; post-write verification failed; escalation required.

**No general true rollback** in baseline. Successor-based recovery is forward recovery, not rollback.

---

## 6. Self-healing boundary

No self-healing approved yet. Prerequisites: finding taxonomy, repair allowlist, plan digest, immutable seal, lock, approval, budgets, circuit breaker, post-repair verify, Recovery/Successor protocol for finalized-run problems. **Never** auto-reconstruct missing JSON SoT from events alone.

---

## 7. Artifact / link inspection

**Deferred** (Task 29). When introduced: advisory only; must not auto-advance lifecycle state.

---

## 8. Event and schema policy (near-term)

Tasks 21–26: **no new lifecycle record or event types**. Reuse canonical APIs where semantics match. Derived plans, approvals, execution receipts, recovery lineage — **not** disguised as existing lifecycle events.

Recovery/Successor protocol (Task 27+) may eventually require new authoritative types — **separate Architect schema task** each time.

---

## 9. Explicit non-goals (Phase 2)

Unchanged from Task 18 planning: no daemon, scheduler, queue, HTR SQLite lifecycle DB, browser automation, silent heal, unattended pipeline, direct raw JSONL/SoT writes by runtime, automatic delegate_task/HEAL loops, changes to frozen 11-record chain.

Additionally: no in-place finalized-run recovery; no ordinary unlock/bypass; no ad hoc recovery-run format before Task 27.

---

## 10. Accepted safe-automation progression

```
read-only observability          ← Task 19 ✅
→ derived action planning        ← Task 21 (next)
→ immutable finalized-run enforcement ← Task 22 (blocks invoke)
→ execution lock/lease           ← Task 23
→ authoritative scoped approval  ← Task 24
→ human-gated single-API invoke  ← Task 25
→ ambiguous-outcome reconciliation ← Task 26
→ Recovery/Successor Run protocol ← Task 27
→ bounded retry and repair       ← Task 28
→ selective unattended automation
→ multi-project orchestration    ← Task 30
→ controlled learning            ← Task 31
```

Human approval is selective (high-risk, low-confidence, recovery-run creation, repair, escalation) — not the default operating model.

---

## 11. Task 18 §11 decisions (resolved at Task 20)

| # | Question | Resolution (Policy C) |
|---|----------|------------------------|
| 1 | Hard lock before write/invoke? | **Immutable finalized-run enforcement (Task 22) required before any Phase 2 lifecycle write/invoke.** Recovery is successor-based; original never reopened via normal path. |
| 2 | Read-only MVP or invoke? | **Task 19 read-only MVP complete.** Invoke deferred until Task 22+ prerequisites. |
| 3 | Artifact inspection in MVP? | **Deferred** (Task 29). |
| 4 | Repair proposal form? | **Derived library/stdout JSON** (Task 21); no lifecycle record/event; persistence deferred; finalized-run recovery proposal = Task 27. |
| 5 | New runtime event type? | **Not for Tasks 21–26.** Future approval/recovery/lineage types need separate schema tasks. |

P2-T0 (boundary acceptance) is **passed** for Task 19. Do not reopen “P2-T0 human checkpoint” as a new implementation task.

---

## 12. Phase 2 task map (Task 20–31)

| Task | Name | Status |
|------|------|--------|
| 19 | Read-only observability | ✅ `57a1ed651` |
| 20 | Immutable finalization + safe automation boundary | ✅ Docs checkpoint (Policy C) |
| 21 | Derived action plan (read-only) | **Next implementation** |
| 22 | Immutable finalized-run enforcement | Required before invoke |
| 23 | Execution lock/lease | |
| 24 | Approval control schema + API | |
| 25 | Human-gated single-API invoke pilot | Pre-finalization only; mandatory post-verify |
| 26 | Execution reconciliation / ambiguous outcomes | |
| 27 | Recovery/Successor Run protocol | Architect schema task |
| 28 | Bounded retry/repair framework | |
| 29 | Advisory artifact/link inspection | |
| 30 | Multi-project registry + isolation | |
| 31 | Case history + controlled learning | |

---

## 13. Confirmation

- Task 19 checkpointed at `57a1ed651d622b3af82939d970b9c7f235ea1764`.
- Phase 2 **implementation has started** (read-only foundation).
- Task 20 records Policy C; **implements no runtime safety mechanism**.
- Finalized-run seal and Recovery/Successor protocol are **defined, not implemented**.
- No Phase 2 lifecycle write path is enabled.
- Phase 1 frozen chain and Task 17.1 historical semantics preserved in §0.
