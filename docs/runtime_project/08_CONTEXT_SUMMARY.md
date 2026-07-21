# Context Summary — HTR (for GPT-5.6-Sol)

**Generated:** 2026-07-21
**Task:** Task 24 — Authoritative approval control (checkpointed)
**Status:** Task 23 checkpointed `c89f1161`; **Task 24 checkpointed**; **Task 25 not started**; all lifecycle invoke disabled

---

## 1. One-paragraph state

Phase 1 remains **semantically closed** at Task 17.1 `8fea4daa0`. Task 19 (`57a1ed651`) delivered read-only observe. Task 20 (`2fa580b5`) accepted **Policy C** (docs). Task 21 delivered read-only derived action plans. Task 22 enforces Policy C immutable seal at all 25 public/run-aware mutation boundaries. Task 23 adds a run-scoped durable write barrier for those same 25 mutators. **Task 24** adds authoritative approval control at `{runs_root}/.control/approvals/{approval_id}/` with immutable issue/revoke/claim/outcome records, a dedicated control-plane write barrier reusing the Task 23 marker, and read/write approval APIs — still **no lifecycle invoke**. Phase 2 lifecycle invoke remains disabled until Task 25.

---

## 2. Policy C (Task 20 architecture; Task 22 enforcement ✅)

| Principle | Meaning |
|-----------|---------|
| **Immutable finalization** | Valid closure → original run sealed against normal HTR mutation (Task 22 ✅) |
| **Recovery/Successor Run** | Remediation in separate linked run (Task 27+) |
| **Write-path gate** | Task 22 seal ✅; Task 23 write barrier ✅; Task 24 approval control ✅ — invoke still blocked until Task 25 |
| **Read-only paths** | Observe and plan allowed on finalized runs; literal zero-write replay/rejection preserved |

---

## 3. Task 24 contract (checkpointed)

| Topic | Rule |
|-------|------|
| **SoT** | `{runs_root}/.control/approvals/{approval_id}/issue.json` (+ optional revoke/claim/outcome); no mutable index; list scans issue records |
| **Legacy** | `{run_root}/approvals.jsonl` is inert bootstrap artifact — not authoritative; Task 24 never reads/writes it |
| **Identity** | Separate `htr.approval.digest.v1`; binds plan digest, observation digest, API, expanded arguments, explicit event_ids, checkpoint, risk, approver/executor, expiry |
| **Records** | Immutable O_EXCL JSON; fsync file + directory; exact replay idempotent; conflicting replay fails closed |
| **Derived states** | expired (from `expires_at`); invalidated (live mismatch); blocked_finalized (valid seal) — advisory in read validation |
| **Finalized run** | issue/new claim rejected on `FINALIZED_VALID`; revoke/outcome after finalization allowed; approval never reopens sealed run |
| **Barrier** | Internal `_approval_control_barrier` in `approval_control.py`; reuses Task 23 marker via `_acquire_outer_run_marker`; no generic seal-disable switch |
| **Task 25 hook** | Internal `_approval_use_session` holds marker across validate → claim → invoke → outcome (invoke not implemented in Task 24) |
| **Not claimed** | lifecycle invoke; reconciliation; Recovery/Successor; self-healing; writes to run-tree `approvals.jsonl` |

---

## 4. Task 23 contract (checkpointed)

| Topic | Rule |
|-------|------|
| **Scope** | All 25 public/run-aware mutators; marker at `{runs_root}/.execution_locks/{run_id}.marker` |
| **Read-only preliminary** | Terminal read-only outcomes or write-intent routing only; preflight never authorizes write |
| **Zero-write paths** | Exact closure replay; finalized rejection; untrusted closure rejection — no bootstrap/markers/events/mtime changes |
| **Write path** | Preliminary → bootstrap → O_EXCL → durability → revalidation → `run_write_started` → mutation → verified cleanup + fsync |
| **Occupied marker** | Always `occupied_unknown`; no takeover/stale cleanup/force/unlock/skip/env bypass |
| **Nesting** | Same-thread/same-Run reuse; other threads/processes blocked; cross-key rejected |
| **Before write started** | No Run write claimed; marker cleaned when possible; cleanup uncertainty fails closed |
| **After write started** | Marker preserved; `mutation_may_have_committed`; `safe_to_retry = false` |
| **First closure** | JSON → private event append under active write context + closure-append guard |
| **Not claimed** | Transactionality; multi-file atomic commit; rollback; reconciliation; auto marker recovery; distributed lock; out-of-band tampering protection |

Verification (Git-only formal isolated archive): full HTR manifest **1487 passed** (26 files); **0 failed**; **0 skipped**.

---

## 5. Phase 1 frozen manual chain (11 records)

Unchanged. Terminal: `run_final_closure_record` / `run_final_closure_recorded`. Task 17.1: chain-terminal; Task 22: global mutation seal after valid finalization.

---

## 6. Accepted Phase 2 progression (Tasks 19–31)

```
19 observe ✅ → 21 action plan ✅ → 22 immutable seal ✅ → 23 write barrier ✅ → 24 approval ✅
→ 25 human-gated invoke (next) → 26 reconciliation → 27 Recovery/Successor
→ 28 bounded repair → 29 artifact inspect → 30 multi-project → 31 learning
```

---

## 7. Before first Phase 2 invoke (still required)

Task 25 re-observe + stale rejection + canonical invoke + post-verification. Task 22 seal ✅. Task 23 write barrier ✅. Task 24 approval control ✅. **Task 25 not started**; lifecycle invoke disabled.

---

Task 17 `939e8b606`. Task 17.1 `8fea4daa0`. Task 18 `f7e291ff7`. Task 18.5 `04b11bc4d`. Task 19 `57a1ed651`. Task 20 `2fa580b5`. Task 21 `798bc1ea`. Task 22 `896961d0`. Task 23 `c89f1161`. **Task 24 checkpointed (this commit).**
