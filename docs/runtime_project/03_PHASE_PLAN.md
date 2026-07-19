# Phase Plan — HTR

**Baseline:** Architecture Baseline v1.0  
**Date:** 2026-07-18  
**Updated:** 2026-07-19 (Phase 1 closed at `8fea4daa0`; Phase 2 planning started)

---

## Phase 0: Baseline & Reconnaissance

**Status:** Complete (documentation + recon)

Scope:

- Project control files under `docs/runtime_project/`
- Repository architecture reconnaissance
- Existing component integration map
- ADR decision records
- Test entry confirmation
- First `08_CONTEXT_SUMMARY.md`

---

## Phase 1: Manual Trusted Run-Record Chain (actual)

**Status:** Closed — Task 17 (`939e8b606`) + Task 17.1 (`8fea4daa0`)  
Phase 1 implementation and post-review hardening are complete. No further Phase 1 lifecycle work.

Phase 1 as delivered is the **manual 11-record run-level workflow** ending at `run_final_closure_record` (JSON SoT + audit events). It is **not** the earlier aspirational “Trusted Task Loop” list below.

**Frozen deliverables (summary):**

- Run/task/attempt IDs, workspace paths, status machines, events
- Manual run-record chain through final closure
- Idempotent SoT guards (event-present / JSON-missing fails closed)
- No Runtime / delegate_task / scheduler / queue / database / HEAL automation in Phase 1

**Earlier aspirational Phase 1 items (deferred, not unlocked by Phase 1 freeze):**

- Minimal `delegate_task` envelope integration
- HEAL creates new attempt and re-verifies
- Full automated trusted task loop E2E

**Explicitly out of scope for Phase 1:** Dashboard, external DB/queue, full rollback, business idempotency, multi-layer delegation, autonomous Task Graph edits, global post-closure hard lock.

---

## Phase 2: Runtime Integration Boundary

**Status:** Planning started (Task 18) — implementation not started — see `09_PHASE2_RUNTIME_BOUNDARY.md`

Phase 2 defines what a controlled runtime integration **may and may not** do before implementation.

**Planning focus:**

1. Closure = chain terminality vs optional whole-run hard lock (hard lock is separate go/no-go)
2. Post-closure task/attempt mutation policy
3. Integrity: fail closed (required); no silent heal; manual repair proposals deferred/open
4. Runtime read/write permissions (MVP: read-oriented)
5. No direct event append / no direct JSON SoT writes by runtime
6. Later writes only through approved lifecycle APIs + human checkpoint (if enabled)
7. Human checkpoint requirements
8. Optional advisory artifact/link inspection (open) — must not auto-advance lifecycle state
9. Explicit non-goals: no daemon, scheduler, queue, database, browser automation, silent self-healing, unattended long-running pipeline, automatic delegate_task/HEAL loops, no changes to the frozen 11-record chain

**Implementation:** not started. Architect must accept `09_PHASE2_RUNTIME_BOUNDARY.md` before any Phase 2 code task.

---

## Phase 3: Domain Reliability (deferred)

Formerly labeled “Phase 2” in baseline notes. Deferred until after Phase 2 runtime boundary acceptance:

- Business idempotency
- Business resource locks (e.g. `ebay_item_id`)
- Full Side-effect Ledger
- Rollback / Compensation
- Simulation / Replay
- Partial success
- Complex dependency graphs

---

## Phase 4: Operations (deferred)

- Dashboard
- Search and audit queries
- HEAL success metrics
- Runtime metrics
- Review queue
- Cost and duration statistics

---

## Current Task Queue Pointer

See `05_TASK_QUEUE.md`. Phase 2 planning document: `09_PHASE2_RUNTIME_BOUNDARY.md`.
