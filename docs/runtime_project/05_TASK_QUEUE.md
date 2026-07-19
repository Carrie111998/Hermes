# Task Queue — HTR

**Last updated:** 2026-07-19 (Task 15 completed by Cursor)

---

## Active Task

None — awaiting Architect review of Task 15.

---

## Completed

### Task 15 — Manual Post-Verification Execution Verification Recording

**Status:** ✅ Complete (awaiting Architect review — not checkpointed)  
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

Task 16 — not started. Await scope assignment.

---

## Backlog

See `03_PHASE_PLAN.md`.
