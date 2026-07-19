# Task Queue — HTR

**Last updated:** 2026-07-20 (Task 19 Phase 2 read-only observe; parent Task 18.5 `04b11bc4d`)

---

## Active Task

None — Task 19 checkpointed; next Phase 2 work awaits Architect assignment (see `09_PHASE2_RUNTIME_BOUNDARY.md`).

---

## Completed

### Task 19 — Read-Only Runtime Observability (Phase 2 first implementation)

**Status:** ✅ Checkpointed (first Phase 2 **implementation** task; builds on Task 18.5 `04b11bc4d`)  
**Tests (candidate Git-only workspace):** focused Task 19 **25 passed**; full tracked `tests/htr/` **1246 passed** (22 files)  
**Depends on:** Task 18.5 `04b11bc4df883ee1039c0d10fab1ede7b2fc0e7e`

**Scope:** Strictly read-only single-run observation and integrity reporting — foundation for later reliable, traceable, recoverable, human-gated automation; **not** a manual-only or permanent read-only architecture.

**Delivered:**

- `htr/observe.py` — deterministic machine-readable snapshot, Phase 1 chain visibility, task/attempt summaries, integrity findings
- `hermes htr observe <run_id>` — JSON-only stdout; `--summary` on stderr; exit 0/1/2 fail-closed contract
- Read-only boundary tests (AST + runtime tree-hash proofs)

**Explicitly excluded:** artifact observation, transition replay, repair/auto-heal, run listing, snapshot persistence, hard-lock enforcement, new lifecycle schemas/records/events; **no** edits to `htr/events.py` / `htr/schemas.py`.

**Frozen semantics preserved:** final closure terminal only for the Phase 1 manual record chain; post-closure activity is advisory (no global hard lock).

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

## Next Task (Architect)

Phase 1 baseline Git-reproducible at Task 18.5 (`04b11bc4d`). Task 19 read-only observe checkpointed.

1. Accept Task 19 or request amendments.
2. Assign next Phase 2 slice per `09_PHASE2_RUNTIME_BOUNDARY.md` (e.g. human checkpoint wiring, repair **proposal** shape — not execution).
3. Deferred: artifact observation, transition replay, `htr/audit.py`, unclear-provenance tests.

---

## Backlog

See `03_PHASE_PLAN.md` and `09_PHASE2_RUNTIME_BOUNDARY.md`.
