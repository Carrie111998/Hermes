# Task Queue — HTR

**Last updated:** 2026-07-19 (Task 17 completed by Cursor)

---

## Active Task

None — awaiting Architect review of Task 17.

---

## Completed

### Task 17 — Phase 1 Boundary / End-to-End Manual Workflow Freeze

**Status:** ✅ Complete (awaiting Architect review — not checkpointed)  
**Tests:** `uv run --extra dev pytest tests/htr/ -v`

Changes:

- Phase 1 boundary constants: `PHASE1_MANUAL_WORKFLOW_RECORD_CHAIN`, `PHASE1_TERMINAL_RECORD_TYPE`, `PHASE1_TERMINAL_EVENT_TYPE`, `PHASE1_BOUNDARY_STATUS`
- `PHASE1_BOUNDARY_STATUS` is a constant/documentation marker only — **not** a lifecycle event
- End-to-end manual workflow regression test through final closure
- Boundary regression tests: no new record/event type, no boundary record file, AST import guards
- **No lifecycle behavior changes** — tests/docs/constants only
- Phase 1 terminal record: `run_final_closure_record`; terminal event: `run_final_closure_recorded`
- 11-record manual chain frozen; JSON records are source-of-truth; event log is audit-only
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
- **Terminal for Phase 1** — no new followup loop, no automatic validation/test execution, no prior record mutation
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

**Status:** ✅ Completed (awaiting Architect review — not checkpointed)  
**Tests:** `python3 -m pytest tests/htr/ -v`

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

Phase 2 planning — not started. Task 17 completes Phase 1; no Phase 2 implementation in Task 17.

---

## Backlog

See `03_PHASE_PLAN.md`.
