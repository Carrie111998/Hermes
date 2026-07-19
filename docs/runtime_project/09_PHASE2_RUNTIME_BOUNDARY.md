# Phase 2 Planning — Runtime Integration Boundary

**Status:** Phase 2 planning started (Task 18) — no implementation  
**Date:** 2026-07-19  
**Depends on:** Phase 1 closed — Task 17 (`939e8b606`) + Task 17.1 (`8fea4daa0`)  
**Audience:** Architect (GPT-5.6-Sol) + Cursor implementer

This document defines what Phase 2 **may** and **may not** do before any runtime integration code is written.  
It does **not** authorize implementation. No changes to `htr/events.py` / `htr/schemas.py` are implied by this plan alone.

---

## 0. Phase 1 baseline (closed)

Phase 1 delivered a manual 11-record run-level chain ending at `run_final_closure_record`.  
Phase 1 implementation and post-review hardening are **closed** at Task 17.1 `8fea4daa0`.

- JSON run-records are source-of-truth (SoT).
- Event log is audit-only.
- Final closure is terminal for the **manual run-record chain**.
- `record_run_final_closure` itself does not mutate `run_manifest` / `task_status` / `attempt_status`.
- Phase 1 does **not** install a global hard lock on later task/attempt APIs.
- Idempotent replay with matching audit event but missing JSON SoT **fails closed** (`InvalidTransition`); no silent heal.

Operators treat `run_final_closure_record.json` as the Phase 1 terminal boundary.

---

## 1. Final closure: chain terminality vs whole-run hard lock

| Option | Meaning | Phase 2 stance |
|--------|---------|----------------|
| **A. Chain terminality only** (Phase 1 today) | Closure ends the manual run-record chain; task/attempt APIs remain callable | Preserve as default until an explicit hard-lock task ships |
| **B. Whole-run hard lock** | After closure JSON exists, refuse task/attempt (and other lifecycle) mutations | **Allowed as a Phase 2 decision**, but only via an explicit task + Architect checkpoint |

**Planning decision (provisional):** Phase 2 treats closure as **chain-terminal by default**. A whole-run hard lock is an **optional, gated** deliverable — not assumed by early Phase 2 read-only runtime work.

**Open decision:** Whether Task sequence item P2-T2 (hard lock) is in-scope for Phase 2 MVP or deferred.

---

## 2. Post-closure task/attempt mutation

| Actor | Allowed in Phase 2 planning? |
|-------|------------------------------|
| Human operator via existing APIs, before hard lock | Technically possible (Phase 1 behavior); **discouraged**; treat as out-of-policy |
| Runtime / automation | **Not allowed** to mutate task/attempt after closure exists |
| After hard lock (if adopted) | **Not allowed** for any caller (human or runtime), except a future Architect-approved repair procedure |

**Planning decision:** Phase 2 runtime must treat presence of `run_final_closure_record.json` as “do not mutate task/attempt.” Enforcement may start as runtime policy (read + refuse) before/without a global API hard lock.

**Open decision:** Whether API-level hard lock (all callers) is required before any runtime write path is enabled.

---

## 3. Integrity issues: fail closed vs auto-heal vs manual repair proposals

| Strategy | Phase 2 stance |
|----------|----------------|
| **Fail closed** | **Required default.** Event/JSON mismatch, fingerprint mismatch, missing SoT → refuse progression |
| **Silent auto-heal** | **Forbidden** (see non-goals) |
| **Manual repair proposals** | **Allowed as a later Phase 2 design topic** — human-readable proposal only; must not auto-write SoT or events |
| **Auto-write missing JSON from event payload** | **Forbidden** in Phase 2 |

**Planning decision:** Integrity remains fail-closed. Any repair is human-driven after Architect-visible proposal (if such a proposal surface is added later). No new lifecycle record/event types are authorized by this planning doc alone.

**Open decision:** Shape of “manual repair proposal” (doc-only note vs future record type) — deferred until Architect chooses; not part of Phase 2 MVP.

---

## 4. Runtime read/write permissions

Phase 2 “runtime” means a controlled integration surface that can observe HTR run state — **not** an autonomous executor.

| Resource | Read | Write |
|----------|------|-------|
| Phase 1 JSON SoT run-records | **Yes** (read-only for MVP) | **No** direct filesystem write |
| Event log (JSONL) | **Yes** (read-only for MVP) | **No** direct append |
| `run_manifest` / `task_status` / `attempt_status` | **Yes** | **No** from runtime in MVP |
| Attempt workspace artifacts | See §8 | **No** SoT mutation from inspection alone |

**Planning decision:** Phase 2 MVP is **read-oriented**. Any write capability requires a separate Architect-approved task and must go through existing (or newly approved) lifecycle APIs — never raw file edits.

---

## 5. Whether runtime may append events

**Planning decision:** Runtime **must not** append events directly to the event log.

- Events may only be produced by calling approved lifecycle APIs that already define append semantics.
- Runtime must not invent event types or write JSONL by hand.

**Open decision:** Whether a future “runtime observed” audit event type is ever wanted (would require a new event type — **out of scope until Architect explicitly requests it**).

---

## 6. Whether runtime may write JSON SoT records

**Planning decision:** Runtime **must not** write JSON SoT files directly.

- SoT writes remain the responsibility of approved lifecycle APIs.
- Phase 2 MVP does not add new SoT record types.
- If Phase 2 later allows runtime to *invoke* an existing manual API, that invocation still requires human checkpoint for irreversible chain advances (§7).

---

## 7. Human checkpoint requirements

Human (or Architect-designated operator) checkpoint is **required** before:

1. Any advancement of the Phase 1 manual run-record chain (including closure).
2. Enabling any runtime **write** or **API-invocation** capability beyond read-only observation.
3. Adopting a whole-run hard lock (policy change affecting all callers).
4. Any integrity repair that rewrites or restores SoT JSON.
5. Expanding runtime to inspect artifacts in a way that can influence verification decisions (§8).

**Planning decision:** Phase 2 does not introduce unattended chain progression. Read-only observation may proceed without per-read checkpoints; state-changing actions always need human checkpoint.

---

## 8. Artifact / link inspection requirements

Phase 1 lifecycle APIs do not inspect `artifact` / `result.json` / `verification_result.json` / docs / test output.

| Capability | Phase 2 stance |
|------------|----------------|
| Runtime lists artifact paths / link metadata for human display | **Allowed** (read-only), if scoped and non-mutating |
| Runtime uses artifact contents to auto-advance lifecycle SoT | **Forbidden** |
| Runtime checksum / link checks that only produce **advisory** reports to humans | **Allowed as optional later task** |
| Binding inspection results into new lifecycle SoT without human approval | **Forbidden** |

**Open decision:** Whether advisory artifact inspection is in Phase 2 MVP or deferred.

---

## 9. Explicit non-goals (Phase 2)

Phase 2 **must not** introduce:

- Daemon / long-lived worker process
- Scheduler
- Queue
- Database / SQLite for HTR lifecycle state
- Browser automation
- Silent self-healing of SoT or events
- Unattended long-running pipeline
- Direct raw JSONL event append by runtime
- Direct raw JSON SoT writes by runtime
- New lifecycle record types (unless Architect opens a dedicated task)
- New lifecycle event types (unless Architect opens a dedicated task)
- `delegate_task` integration as an automatic executor
- HEAL/DECO autonomous repair loops
- Subprocess/HTTP execution adapters beyond what Phase 1 already manually gates
- Changing the frozen Phase 1 11-record chain order or terminal record

Former baseline ideas labeled “Phase 2: Domain Reliability” (business locks, compensation graphs, etc.) are **deferred** — see `03_PHASE_PLAN.md`. They are not unlocked by this boundary plan.

---

## 10. Proposed Phase 2 task sequence

All tasks below are **planning placeholders**. None are authorized to start until Architect assigns a task card.

| ID | Task | Intent | Implementation? |
|----|------|--------|-----------------|
| **P2-T0** | Architect accept this boundary doc | Freeze Phase 2 may/may-not rules | Docs only |
| **P2-T1** | Runtime read-only observer | Safe read of SoT JSON + events + status snapshots; no writes | Code only after T0 |
| **P2-T2** | (Optional) Post-closure hard lock | API refuse task/attempt mutation when closure JSON exists | Explicit Architect go/no-go |
| **P2-T3** | Human-gated API invoke surface | Runtime may *propose* calling existing lifecycle APIs; human must approve each SoT-advancing call | No direct SoT/event IO |
| **P2-T4** | Integrity reporting | Surface fail-closed mismatches (event vs JSON, fingerprint) as human-readable reports | No auto-heal |
| **P2-T5** | (Optional) Advisory artifact/link inspection | Read-only checks for humans; never auto-write SoT | Architect go/no-go |
| **P2-T6** | Checkpoint gate + acceptance tests | Prove non-goals (no daemon/scheduler/queue/db/browser/silent heal) | Tests + docs |

**Suggested MVP slice:** P2-T0 → P2-T1 → P2-T4 → P2-T6.  
Defer P2-T2 / P2-T3 / P2-T5 until Architect chooses.

---

## 11. Open decisions (Architect)

1. Is whole-run hard lock (P2-T2) required before any runtime write/invoke path?
2. Is Phase 2 MVP strictly read-only (recommended), or does it include human-gated API invoke (P2-T3)?
3. Is advisory artifact/link inspection in MVP or deferred?
4. What form should “manual repair proposals” take (doc note vs future record type)?
5. When (if ever) may a new runtime-related event type be introduced?

Until these are decided, Cursor must not start runtime implementation.

---

## 12. Confirmation

- This file is **planning only** (Phase 2 planning has started; Phase 2 implementation has not).
- No runtime implementation is started by publishing or updating this document.
- No daemon/scheduler/queue/database/browser automation is authorized.
- No automatic delegate_task/HEAL loops; no silent self-healing; no unattended long-running pipeline.
- No changes to the frozen Phase 1 11-record chain.
- No new lifecycle record/event types are authorized by this document alone.
