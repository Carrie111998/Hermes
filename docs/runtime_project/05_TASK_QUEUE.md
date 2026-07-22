# Task Queue — HTR

**Last updated:** 2026-07-22 (Task 25 ready for checkpoint; parent Task 24.1 `40f4d016`)

---

## Active Task

**Task 26 — Ambiguous outcome reconciliation** — **not started**. Task 25 human-gated invoke pilot is implemented and ready for checkpoint on base `40f4d016`.

**General Phase 2 lifecycle invoke remains disabled** outside the narrow Task 25 pilot API (`complete_run_manually` only). No CLI, generic invoke router, unattended execution, retry, repair, or marker recovery.

---

## Completed (pending checkpoint)

### Task 25 — Human-gated single-API invoke pilot

**Status:** ✅ Implemented — ready for checkpoint (parent Task 24.1 `40f4d01638f3d2f3c16c9c8ef451ab1c20fc21f0`)
**Depends on:** Task 24.1 `40f4d016`

**Delivered:**

- `htr/invoke_run_completion.py` — `invoke_approved_run_completion` pilot bound to **`complete_run_manually` only**
- One continuous `_approval_use_session`: approval claim, lifecycle invoke, mandatory post-observe verification, outcome v2 (`consumed` | `ambiguous`)
- Outcome v2 binds reason and diagnostic evidence; `safe_to_retry=false`; non-null external `project_repository_checkpoint` fail-closed
- `consumed` requires complete verification; `ambiguous` is fail-stop and non-retryable
- No CLI, generic invoke router, retry, reconciliation, marker recovery, or Recovery/Successor Runs

**Tests (formal Git-only isolated archive, pre-commit, zero retries):** full HTR manifest **1623 passed** (27 files); **0 failed**; **0 skipped**

**Explicitly not implemented:** Task 26 reconciliation; general/unattended/multi-API lifecycle invocation

---

## Completed

### Task 24.1 — Execution-Lock Contention Test Harness Repair

**Status:** ✅ Checkpointed (test-only; parent Task 24 `af4868054b0a61fa0511241d58411d16780daa6b`)
**Production diff:** empty — no production modules changed
**Depends on:** Task 24 `af4868054b0a61fa0511241d58411d16780daa6b`

**Context:** Task 24 production checkpoint (`af4868054`) passed pre-commit formal verification with file-retry masking. Its first strict no-retry post-commit archive run exposed a **pre-existing synchronization defect** in `test_concurrent_bootstrap_succeeds` (timing-based release allowed sequential re-acquisition). Parent-versus-child diagnosis on `af4868054` vs `c89f1161` proved equivalent behavior and **no production regression**; held-marker safety (`test_subprocess_o_excl_race_exactly_one_winner`) passed 50/50 on both commits.

**Delivered:**

- `tests/htr/test_execution_lock.py` — `test_concurrent_bootstrap_succeeds` now uses a `release_gate` so the winning worker retains marker ownership until all challengers report (same pattern as `test_subprocess_o_excl_race_exactly_one_winner`)

**Explicitly not implemented:** lifecycle invoke (Task 25), production changes, Task 25 work

---

### Task 24 — Authoritative Approval Control

**Status:** ✅ Checkpointed (fifth Phase 2 **implementation**; commit `af4868054b0a61fa0511241d58411d16780daa6b`; parent Task 23 `c89f1161968931e329f64acb350b166ec564c174`)
**Tests (formal Git-only isolated archive, pre-commit with file-retry):** full HTR manifest **1487 passed** (26 files: Task 23 **1400** + approval-control **87**); **0 failed**; **0 skipped**
**Post-commit note:** first strict no-retry archive run exposed pre-existing flake in `test_concurrent_bootstrap_succeeds` — repaired in Task 24.1 (test-only); production unchanged
**Depends on:** Task 23 `c89f1161968931e329f64acb350b166ec564c174`

**Delivered:**

- `htr/approval_control.py` — authoritative approval SoT at `{runs_root}/.control/approvals/{approval_id}/` with immutable `issue.json`, optional `revoke.json`, singleton `claim.json`, singleton `outcome.json`
- Read APIs: `get_approval`, `list_approvals`, `validate_approval` (advisory only)
- Write APIs under internal `_approval_control_barrier`: `issue_approval`, `revoke_approval`, `claim_approval`, `record_use_outcome`
- Separate `htr.approval.digest.v1` projection; mandatory `expires_at` (max 24h); explicit `event_id` for event-appending APIs
- `{run_root}/approvals.jsonl` documented as inert legacy bootstrap — never read/written by Task 24
- `htr/execution_lock.py` — shared `_acquire_outer_run_marker` helper only; Task 23 `run_write_barrier` seal semantics unchanged
- `htr/paths.py`, `htr/state.py`, `htr/__init__.py` — control-plane paths and approval error types
- `tests/htr/test_approval_control.py` — approval-control hardening matrix (**87 tests**)

**Explicitly not implemented:** lifecycle invoke (Task 25), ambiguous reconciliation (Task 26), Recovery/Successor Runs (Task 27)

---

### Task 23 — Durable Run Write Barrier

**Status:** ✅ Checkpointed (fourth Phase 2 **implementation**; parent Task 22 `896961d0cfbd5a5cce97fc44ad88bf23ec0619eb`)
**Tests (candidate Git-only workspace):** focused execution-lock **37 passed**; finalization **59 passed**; finalization + Task 19/21 **175 passed**; full tracked `tests/htr/` **1400 passed** (25 files)
**Depends on:** Task 22 `896961d0cfbd5a5cce97fc44ad88bf23ec0619eb`

**Delivered:**

- `htr/execution_lock.py` — run-scoped durable write marker (`{runs_root}/.execution_locks/{run_id}.marker`); O_EXCL acquisition; `@run_mutation_boundary` / `run_write_barrier`; `begin_run_write()`; closure-append guard; ownership-checked release
- `htr/events.py`, `htr/io.py`, `htr/contracts.py`, `htr/artifacts.py` — all 25 public/run-aware mutators wired through the barrier
- `tests/htr/test_execution_lock.py` — runtime write-path matrix (25/25); subprocess crash/race/fork tests; path/release tests; literal zero-write proofs
- `tests/htr/test_finalization.py` — literal project zero-write snapshots for finalized/untrusted/replay rejection

**Contract (Task 23):**

- Run-scoped durable write barrier for all 25 committed public/run-aware mutators on supported POSIX/Linux local filesystem
- Read-only preliminary seal classification may only produce terminal read-only outcomes or route toward write intent; preflight never authorizes a write
- Literal zero-filesystem-write paths: exact final-closure replay; preliminary finalized rejection; preliminary suspicious/untrusted closure rejection — no bootstrap, `.execution_locks`, markers, events, or mtime changes
- Write path: read-only preliminary classification → bootstrap → O_EXCL marker → durability → authoritative revalidation → `run_write_started` before first possible Run write → mutation → ownership-checked marker removal + directory fsync
- Existing marker always `occupied_unknown`; no automatic stale cleanup, takeover, force, unlock, skip, env bypass, or public release API
- Same-thread/same-Run nested calls reuse outer marker; other threads/processes not reentrant; cross-key nesting rejected
- Failure before `run_write_started`: no Run write claimed; owned marker cleaned when possible; cleanup uncertainty fails closed
- Failure after `run_write_started`: marker preserved; `mutation_may_have_committed = true`; `safe_to_retry = false`
- First final closure: closure JSON → private final-closure event append while holding active write context; `_append_run_event_internal` requires active ownership, PID/thread/key/token match, positive nested depth, `run_write_started`, and narrow closure-append context
- Observe and plan remain lock-free, read-only, unchanged
- Does **not** claim: database transactionality; atomic multi-file commit; rollback; ambiguous-outcome reconciliation; safe automatic marker recovery; distributed locking; protection against deliberate same-user out-of-band tampering

**Explicitly not implemented:** Task 24 approval, Task 25 invoke, Task 26 reconciliation/marker-residue handling, Task 27 Recovery/Successor Run, Phase 2 lifecycle invocation.

### Task 22 — Immutable Finalized-Run Enforcement

**Status:** ✅ Checkpointed (third Phase 2 **implementation**; parent Task 21 `798bc1ea98b6af8904c9750102c7bfe3917cdfe0`)
**Tests (candidate Git-only workspace):** focused Task 22 **56 passed**; finalization + Task 19/21 **135 passed**; full tracked `tests/htr/` **1360 passed** (24 files)
**Depends on:** Task 21 `798bc1ea98b6af8904c9750102c7bfe3917cdfe0`

**Delivered:**

- `htr/finalization.py` — focused read-only seal evaluator (`not_finalized`, `finalized_valid`, `closure_present_untrusted`, `indeterminate`); `assert_run_mutation_allowed()`; closure event/record matcher; path containment
- `htr/state.py` — `RunFinalizedError`, `RunSealBlockedError` with stable error codes
- Guards on all 25 public/run-aware mutation entry points (workspace, task/attempt, artifacts, events, eleven Phase 1 run-chain APIs)
- `htr/events.py` — JSON-before-event first closure; private `_append_run_event_internal` (validated first-closure path only); public append rejects `run_final_closure_recorded`; exact `record_run_final_closure` replay is sole zero-write replay exception
- `tests/htr/test_finalization.py` — 25/25 mutation callables individually runtime-tested; untrusted-state matrix; guard-order proofs; import smoke

**Contract (Task 22):**

- Valid final closure permanently seals the original run against all normal committed HTR mutation APIs
- Read-only observation (`hermes htr observe`) and read-only planning (`hermes htr plan`) remain allowed
- Valid closure requires trusted JSON/event correspondence and valid preceding frozen chain
- Closure-present-but-untrusted and indeterminate states fail closed (`RunSealBlockedError`); no automatic repair or event-to-JSON reconstruction
- No force, unlock, env-var, low-level helper, or ordinary reopening bypass
- Exact `record_run_final_closure` replay (matching event ID + semantics) returns existing record with zero writes; all other normal mutation/idempotent replay blocked after finalization
- Generic filesystem primitives (`atomic_write_json`, `append_jsonl`, `ensure_dir`) and deliberate manual edits are **not** claimed protected
- Cross-process TOCTOU between seal check and write remains (Task 23 scope)
- Recovery/Successor Run protocol remains Task 27+; Phase 2 lifecycle invoke remains disabled

**Explicitly not implemented:** Task 23 lock/lease, Task 24 approval, Task 25 invoke, Recovery/Successor Run, self-healing, bypass mechanisms.

### Task 21 — Derived Action Plan Generation (read-only)

**Status:** ✅ Checkpointed (second Phase 2 **implementation**; parent Task 20 `2fa580b5f8b5d26657af2af5641724515e114c76`)
**Tests (candidate Git-only workspace):** focused Task 21 **60 passed**; full tracked `tests/htr/` **1304 passed** (23 files)
**Depends on:** Task 20 `2fa580b5f8b5d26657af2af5641724515e114c76`

**Delivered:**

- `htr/action_plan.py` — Hybrid D derived planner on Task 19 snapshots; semantic observation projection + digests; eleven-action frozen Phase 1 catalog; Policy C planning states; no lifecycle import
- `hermes htr plan <run_id>` — JSON stdout; `--summary` stderr; exit 0/1/2; `--runs-root` supplies canonical `project_dir` binding where required
- `tests/htr/test_action_plan.py`, extended `test_phase2_read_only_boundary.py` — contract, digest, Policy C, path-binding, idempotency, runtime tree-hash proofs

**Contract preserved:**

- Strictly read-only — no invoke, append, SoT write, lock, approval, recovery, subprocess, or network
- `proposable` = semantically complete planning proposal only — **not** executable or currently authorized; event identity may remain unbound until invoke
- Policy C at planning layer: finalized original-run mutation → `blocked_finalized`; explicit remediation intent → `recovery_protocol_required`
- Committed `project_dir` = HTR runs-storage root (same role as observer `base_dir` / `--runs-root`); not project repository or run workspace

**Explicitly not implemented:** Task 22 seal, Task 23 lock, Task 24 approval, Task 25 invoke, Recovery/Successor Run (Task 27+), execution authorization.

### Task 20 — Immutable Finalization and Safe Automation Control Boundary

**Status:** ✅ Architecture checkpoint (docs only; Policy C accepted; parent Task 19 `57a1ed651`)
**Tests:** n/a (documentation only)
**Depends on:** Task 19 `57a1ed651d622b3af82939d970b9c7f235ea1764`

**Delivered (documentation only — no runtime code):**

- Accepted **Policy C:** immutable **finalized-run seal** (future Task 22) + **Recovery/Successor Run** (future Task 27) — no in-place reopen/unlock of original runs
- Resolved Task 18 §11 decisions; corrected stale Phase 2 status in `09_PHASE2_RUNTIME_BOUNDARY.md`
- Write-path gate: **no Phase 2 lifecycle write/invoke before Task 22**
- Accepted task sequence Tasks 21–31; Task 21 next (read-only action plan)

**Explicitly not implemented:** finalized-run enforcement, approval storage, lock/lease, invoke, recovery protocol, bypass/unlock mechanisms.

**Historical compatibility:** Task 17.1 semantics preserved — Phase 1 closure was chain-terminal only; Policy C is future Phase 2 enforcement, not retroactive code change.

### Task 19 — Read-Only Runtime Observability (Phase 2 first implementation)

**Status:** ✅ Checkpointed `57a1ed651d622b3af82939d970b9c7f235ea1764` (first Phase 2 **implementation**; builds on Task 18.5 `04b11bc4d`)
**Tests (candidate Git-only workspace):** focused Task 19 **25 passed**; full tracked `tests/htr/` **1246 passed** (22 files)
**Depends on:** Task 18.5 `04b11bc4df883ee1039c0d10fab1ede7b2fc0e7e`

**Scope:** Strictly read-only single-run observation and integrity reporting — foundation for later reliable, traceable, recoverable, human-gated automation; **not** a manual-only or permanent read-only architecture.

**Delivered:**

- `htr/observe.py` — deterministic machine-readable snapshot, Phase 1 chain visibility, task/attempt summaries, integrity findings
- `hermes htr observe <run_id>` — JSON-only stdout; `--summary` on stderr; exit 0/1/2 fail-closed contract
- Read-only boundary tests (AST + runtime tree-hash proofs)

**Explicitly excluded:** artifact observation, transition replay, repair/auto-heal, run listing, snapshot persistence, hard-lock enforcement, new lifecycle schemas/records/events; **no** edits to `htr/events.py` / `htr/schemas.py`.

**Frozen semantics preserved (Task 17.1 historical):** final closure terminal for Phase 1 manual chain; post-closure activity advisory; **current APIs do not yet enforce Policy C immutable seal** (Task 22).

### Task 18.5 — Reconcile Phase 1 Tracked Baseline

**Status:** ✅ Checkpointed `04b11bc4df883ee1039c0d10fab1ede7b2fc0e7e` (additive only; parent `f7e291ff7`)
**Tests (candidate Git-only workspace):** `tests/htr/` — **1221 passed** (20 files: 8 foundation + 12 Phase 1 workflow)
**Depends on:** Task 18 `f7e291ff7`

**Problem:** Phase 1 workflow semantics were closed and tested locally, but Git reproducibility was broken from the first tracked HTR commit: five foundation modules and eight foundation tests were never checkpointed.

**Changes (byte-for-byte admission; no semantic edits):**

- Production: `htr/paths.py`, `htr/ids.py`, `htr/io.py`, `htr/state.py`, `htr/artifacts.py`
- Tests: `tests/htr/test_paths.py`, `test_ids.py`, `test_io.py`, `test_state.py`, `test_artifacts.py`, `test_contracts.py`, `test_events.py`, `test_schemas.py`

**Explicitly excluded (deferred):** `htr/audit.py`, `tests/htr/test_verification.py`, `tests/htr/test_run_completion.py`, all Task 19 paths.

**Frozen / unchanged:** Phase 1 lifecycle, 11-record chain, `htr/contracts.py`, `htr/events.py`, `htr/schemas.py`, frozen workflow tests. Prior checkpoints not rewritten.

**Note:** Semantic closure predated Git reproducibility; Task 18.5 restores Git-only reproducibility without redesign.

### Task 18 — Phase 2 Runtime Boundary Planning (docs only)

**Status:** ✅ Complete (checkpointed `f7e291ff7`)
**Tests:** n/a (docs only; no code/schema/events changes)
**Depends on:** Phase 1 closed at Task 17.1 `8fea4daa0`

Changes:

- Added `docs/runtime_project/09_PHASE2_RUNTIME_BOUNDARY.md`
- Updated `03_PHASE_PLAN.md` to reflect Phase 1 actual freeze + Phase 2 = runtime boundary planning
- Deferred former “Domain Reliability” content to Phase 3
- Status cleanup: Task 17.1 checkpointed; Phase 1 implementation/post-review hardening closed; Phase 2 planning started; Phase 2 implementation not started
- **No implementation** — no runtime, scheduler, queue, database, delegate_task, browser automation
- **No** new lifecycle record/event types; **no** edits to `htr/events.py` / `htr/schemas.py`

### Task 17.1 — Clarify Phase 1 Terminal Semantics and Guard Idempotent SoT

**Status:** ✅ Accepted (checkpointed `8fea4daa0`)
**Tests:** `uv run --extra dev pytest tests/htr/ -v` — **1273 passed**
**Builds on:** Task 17 checkpoint `939e8b606de09532006887c637684cf8baa49d40`

Changes:

- Docs: final closure is terminal for the Phase 1 **manual run-record chain** only
- Docs: `record_run_final_closure` preserves `run_manifest` / `task_status` / `attempt_status` snapshots
- Docs: Phase 1 does **not** install a global hard lock on later task/attempt APIs; operators treat `run_final_closure_record.json` as the boundary; Phase 2 may add a hard lock later
- `htr/events.py`: idempotent replay of manual run-record APIs requires JSON SoT file; event-present / JSON-missing → `InvalidTransition` (no silent heal)
- Tests: rename overclaiming “terminal” wording; add event-present / JSON-missing regression tests
- No new record/event types; no Phase 1 chain change; no global post-closure hard lock
- Closes Phase 1 implementation / post-review hardening

### Task 17 — Phase 1 Boundary / End-to-End Manual Workflow Freeze

**Status:** ✅ Accepted (checkpointed `939e8b606`)
**Tests:** `uv run --extra dev pytest tests/htr/ -v` — **1271 passed** at Phase 1 final verification

Changes:

- Phase 1 boundary constants: `PHASE1_MANUAL_WORKFLOW_RECORD_CHAIN`, `PHASE1_TERMINAL_RECORD_TYPE`, `PHASE1_TERMINAL_EVENT_TYPE`, `PHASE1_BOUNDARY_STATUS`
- `PHASE1_BOUNDARY_STATUS` is a constant/documentation marker only — **not** a lifecycle event
- End-to-end manual workflow regression test through final closure
- Boundary regression tests: no new record/event type, no boundary record file, AST import guards
- Phase 1 terminal record: `run_final_closure_record`; terminal event: `run_final_closure_recorded`
- 11-record manual chain frozen; JSON records are source-of-truth; event log is audit-only
- Final closure is terminal for the manual run-record chain (not a global task/attempt hard lock)
- No Runtime/delegate_task/scheduler/queue/database/HEAL/DECO; no automation in Phase 1

### Task 16 — Run Final Closure Record

**Status:** ✅ Accepted (checkpointed `1650b9e73`)
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- `run_final_closure_record` contract + schema validation
- `make_run_final_closure_record`, `run_final_closure_fingerprint`
- `validate_run_final_closure_sources_correspond`, `compute_run_final_closure_status`
- `record_run_final_closure()` — manual final closure after full Phase 1 workflow chain
- Fingerprints must match all 10 prior run-level records
- `closure_items` must correspond to post-verification execution verification items (or be global/manual)
- Writes `run_final_closure_record.json`, appends `run_final_closure_recorded` event
- **Terminal for Phase 1 manual run-record chain** — no new followup loop, no automatic validation/test execution, no prior record mutation by this API
- Does not install a global hard lock on later task/attempt APIs (Phase 1)
- Final closure statuses: `closed_verified`, `closed_rejected`, `closed_needs_more_work`, `closed_no_action`
- No artifact/result/verification_result/docs/test-output inspection

### Task 15 — Manual Post-Verification Execution Verification Recording

**Status:** ✅ Accepted (checkpointed `5011ad44c`)
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- `run_post_verification_execution_verification_record` contract + schema validation
- `make_run_post_verification_execution_verification_record`, `run_post_verification_execution_verification_fingerprint`
- `validate_post_verification_execution_verification_items_correspond`, `compute_post_verification_execution_verification_status`
- `record_post_verification_execution_verification()` — manual verification recording after post-verification execution result exists
- Fingerprints must match on-disk result + verification + post-verification follow-up plan + post-verification execution request + post-verification execution result records
- `verification_items` must correspond to post-verification execution result items (or be global/manual)
- Writes `run_post_verification_execution_verification_record.json`, appends `run_post_verification_execution_verification_recorded` event
- **Recording only** — no automatic verification, no test execution, no prior record mutation, no task/attempt creation
- Empty post-verification execution result normally produces `empty` verification; completed/failed/partial result may produce `verified`/`rejected`/`needs_changes` verification
- No artifact/result/verification_result/docs/test-output inspection

### Task 14 — Manual Post-Verification Execution Result Recording

**Status:** ✅ Accepted (checkpointed)
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- `run_post_verification_execution_result_record` contract + schema validation
- `make_run_post_verification_execution_result_record`, `run_post_verification_execution_result_fingerprint`
- `validate_post_verification_execution_result_items_correspond`
- `record_post_verification_execution_result()` — manual result recording after post-verification execution request exists
- Fingerprints must match on-disk result + verification + post-verification follow-up plan + post-verification execution request records
- `result_items` must correspond to post-verification execution request items (or be global/manual)
- Writes `run_post_verification_execution_result_record.json`, appends `run_post_verification_execution_result_recorded` event
- **Recording only** — no execution, no prior record mutation, no task/attempt creation
- Empty post-verification execution request normally produces `empty` result; requested request may produce `completed`/`failed`/`partial` result
- No artifact/result/verification_result/docs inspection

### Task 13 — Manual Post-Verification Execution Request Planning

**Status:** ✅ Accepted (checkpointed)
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- `run_post_verification_execution_request_record` contract + schema validation
- `make_run_post_verification_execution_request_record`, `run_post_verification_execution_request_fingerprint`
- `derive_post_verification_execution_request_items`, `validate_post_verification_execution_request_items_correspond`
- `request_post_verification_execution()` — planning after post-verification follow-up plan exists
- Fingerprints must match on-disk result + verification + post-verification follow-up plan records
- `request_items` must correspond to post-verification follow-up plan items (or be global/manual)
- Writes `run_post_verification_execution_request_record.json`, appends `run_post_verification_execution_requested` event
- **Planning only** — no execution, no prior record mutation, no task/attempt creation
- Empty post-verification follow-up plan normally produces `empty` request; planned follow-up items may produce `requested` items
- No artifact/result/verification_result inspection

### Task 12 — Verification-Driven Follow-up Planning

**Status:** ✅ Accepted (checkpointed `16d81a65f`)
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- `run_post_verification_followup_plan_record` contract + schema validation
- `make_run_post_verification_followup_plan_record`, `run_post_verification_followup_plan_fingerprint`
- `derive_post_verification_followup_items`, `validate_post_verification_followup_items_correspond`
- `plan_post_verification_followup()` — planning after execution verification record exists
- Fingerprints must match on-disk result + verification records
- `followup_items` must correspond to execution items + item verifications
- Writes `run_post_verification_followup_plan_record.json`, appends `run_post_verification_followup_planned` event
- **Planning only** — no execution, no prior record mutation, no task/attempt creation
- Accepted verification normally produces `empty` plan; rejected/needs_changes produce `planned` items
- No artifact/result/verification_result inspection

### Task 11 / Task 10 / Task 0–9

**Status:** ✅ Accepted (checkpointed)

---

## Next Task (Implementer)

**Task 25 — Human-gated single-API invoke pilot.** Required before any Phase 2 lifecycle invoke path is enabled (Task 22 seal ✅; Task 23 write barrier ✅; Task 24 approval control ✅).

**All lifecycle invoke remains disabled** until Task 25 is implemented.

See `09_PHASE2_RUNTIME_BOUNDARY.md` for Tasks 25–31.

---

## Task 24 — Authoritative Approval Control (checkpointed)

**Status:** ✅ Checkpointed

**Delivered:**

- `htr/approval_control.py` — authoritative approval SoT at `{runs_root}/.control/approvals/{approval_id}/` with immutable `issue.json`, optional `revoke.json`, singleton `claim.json`, singleton `outcome.json`
- Read APIs: `get_approval`, `list_approvals`, `validate_approval` (advisory only)
- Write APIs under internal `_approval_control_barrier`: `issue_approval`, `revoke_approval`, `claim_approval`, `record_use_outcome`
- Separate `htr.approval.digest.v1` projection; mandatory `expires_at` (max 24h); explicit `event_id` for event-appending APIs
- `{run_root}/approvals.jsonl` documented as inert legacy bootstrap — never read/written by Task 24
- `htr/execution_lock.py` — shared `_acquire_outer_run_marker` helper only; Task 23 seal semantics unchanged
- Internal `_approval_use_session` hook for Task 25 continuous marker reuse (invoke not implemented)
- `tests/htr/test_approval_control.py` — approval-control hardening matrix (**87 tests**)

**Tests (formal Git-only isolated archive):** **1487 passed** (26 files); **0 failed**; **0 skipped**

**Explicitly not implemented:** lifecycle invoke (Task 25), ambiguous reconciliation (Task 26), Recovery/Successor Runs (Task 27)

---

## Previous next task (superseded)

**Task 24 — Authoritative Approval Control.** Required before Phase 2 human-gated lifecycle invoke (Task 22 seal ✅; Task 23 write barrier ✅).

**Blocked until Task 24:** human-gated lifecycle invoke (Task 25).

See `09_PHASE2_RUNTIME_BOUNDARY.md` for Tasks 24–31.

---

## Backlog

See `03_PHASE_PLAN.md` and `09_PHASE2_RUNTIME_BOUNDARY.md`.
