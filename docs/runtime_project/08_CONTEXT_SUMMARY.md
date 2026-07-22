# Context Summary — HTR (for GPT-5.6-Sol)

**Generated:** 2026-07-22
**Task:** Task 26A — Read-only execution reconciliation inspection (checkpoint approved)
**Status:** Task 25 checkpointed `c6a9e305`; **Task 26A checkpoint approved** — read-only reconciliation inspection complete; **Task 26B/26C not started/not approved**; retry, repair, and marker disposition remain prohibited; **entire Task 26 not complete**; general lifecycle invoke remains disabled outside Task 25 pilot API

---

## 1. One-paragraph state

Phase 1 remains **semantically closed** at Task 17.1 `8fea4daa0`. Task 19 (`57a1ed651`) delivered read-only observe. Task 20 (`2fa580b5`) accepted **Policy C** (docs). Task 21 delivered read-only derived action plans. Task 22 enforces Policy C immutable seal at all 25 public/run-aware mutation boundaries. Task 23 adds a run-scoped durable write barrier for those same 25 mutators. **Task 24** (`af4868054`) adds authoritative approval control. **Task 24.1** (`40f4d016`) repaired the execution-lock contention test harness. **Task 25** (`c6a9e305`) adds `invoke_approved_run_completion` for approved `complete_run_manually` only. **Task 26A** (checkpoint approved) adds read-only `inspect_run_completion_reconciliation` — derived evidence only; no case persistence, marker disposition, retry, or Recovery Run. **Task 26B/26C not started.**

---

## 2. Policy C (Task 20 architecture; Task 22 enforcement ✅)

| Principle | Meaning |
|-----------|---------|
| **Immutable finalization** | Valid closure → original run sealed against normal HTR mutation (Task 22 ✅) |
| **Recovery/Successor Run** | Remediation in separate linked run (Task 27+) |
| Write-path gate | Task 22 seal ✅; Task 23 write barrier ✅; Task 24 approval + Task 25 invoke ✅ (`c6a9e305`); Task 26A read-only inspection ✅ (checkpoint approved) |
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
| **Task 25 hook** | `_approval_use_session` holds marker across validate → claim → invoke → verification → outcome v2 |
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
→ 25 human-gated invoke ✅ (checkpoint pending) → 26 reconciliation (next) → 27 Recovery/Successor
→ 28 bounded repair → 29 artifact inspect → 30 multi-project → 31 learning
```

---

## 7. Task 25 contract (implemented — ready for checkpoint)

| Topic | Rule |
|-------|------|
| **Pilot API** | `complete_run_manually` only via `invoke_approved_run_completion` |
| **No router/CLI** | No generic lifecycle invoke route; no automatic API selection |
| **Session** | Claim, lifecycle invoke, verification, and outcome under one continuous approval-use marker session |
| **Verification** | `consumed` requires full post-invoke verification; `ambiguous` is fail-stop, non-retryable |
| **Outcome v2** | Evidence-bearing `consumed` / `ambiguous`; v1 read compatibility preserved |
| **Checkpoint** | Non-null external `project_repository_checkpoint` unsupported / fail-closed |
| **Not implemented** | Task 26 reconciliation; Recovery Run; marker takeover/cleanup; unattended execution |

Task 24.1 completed at **`40f4d016`**. Task 25 formal Git-only result: **1623 passed** (27 files); **0 failed**; **0 skipped**; zero retries.

---

## 8. Before broader Phase 2 invoke

Task 25 covers re-observe + stale rejection + single-API invoke + post-verification for `complete_run_manually` only. **Task 26 reconciliation has not started.** General, unattended, and multi-API lifecycle invocation remain disabled.

---

Task 17 `939e8b606`. Task 17.1 `8fea4daa0`. Task 18 `f7e291ff7`. Task 18.5 `04b11bc4d`. Task 19 `57a1ed651`. Task 20 `2fa580b5`. Task 21 `798bc1ea`. Task 22 `896961d0`. Task 23 `c89f1161`. Task 24 production `af4868054`. **Task 24.1: test harness repair (this checkpoint).**
