# Hermes v2 Intake Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Work Contract rejection diagnosable by safe path code and make qualification retry/deduplication bounded and race-safe.

**Architecture:** Keep cryptographic verification in `kanban_intake.py`, but replace its boolean with an immutable result. The DB owns attempt budgets and the partial unique index; API, dashboard, and CLI expose only authenticated actions already permitted by that durable state.

**Tech Stack:** Python dataclasses/literals, HMAC/SHA-256, SQLite JSON1 and partial indexes, FastAPI, dashboard JavaScript, pytest through `scripts/run_tests.sh`.

## Global Constraints

- Verifier output is only `valid` plus one safe failure path; never expose payload, canonical JSON, key, signature, or digest.
- `OSError` must not share an exception branch with `TypeError` or schema/canonicalization errors.
- No failed signed-envelope retention is added.
- Existing Work Contract digest, qualifier revision, evidence digest, and task CAS protections remain intact.
- Retry uses the original intake and cannot reset historical attempt count.
- One active requalification intake exists per target across `pending`, `running`, `needs_clarification`, and `attention_required`.
- Retry actions are visible and callable only by the original Work Inbox principal or an explicit operator surface.
- This plan is independent of workflow/Epic integration and may land in parallel.
- Use `scripts/run_tests.sh`; never invoke `pytest` directly.

---

### Task 1: Return typed Work Contract verification paths

**Files:**
- Modify: `hermes_cli/kanban_intake.py` (`verify_work_contract`, `materialization_fields`)
- Modify: `tests/hermes_cli/test_kanban_intake.py`

**Interfaces:**
- Consumes: signed contract mapping and optional secret/Hermes home.
- Produces: `WorkContractVerification(valid: bool, failure: WorkContractFailure | None)`.

- [ ] **Step 1: Write a failing table of every verifier result**

```python
@pytest.mark.parametrize((mutator, expected), [
    (break_shape, "shape"),
    (break_canonical, "canonical_mismatch"),
    (break_digest, "digest_mismatch"),
    (break_signature, "signature_mismatch"),
])
def test_verify_work_contract_returns_exact_failure(mutator, expected):
    signed = sign_work_contract(_contract(), secret=SECRET)
    result = verify_work_contract(mutator(signed), secret=SECRET)
    assert result == WorkContractVerification(valid=False, failure=expected)
```

Add separate filesystem cases for missing/invalid/unsafe/unreadable key → `key_unreadable` and an injected non-key read/process error → `io_error`. Prove a read-only key path cannot report `signature_mismatch`.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py -k 'verify_work_contract' -q`

Expected: FAIL because the function returns `bool`.

- [ ] **Step 3: Implement the result and explicit exception mapping**

```python
WorkContractFailure = Literal[
    "shape", "canonical_mismatch", "digest_mismatch",
    "signature_mismatch", "key_unreadable", "io_error",
]


@dataclass(frozen=True)
class WorkContractVerification:
    valid: bool
    failure: WorkContractFailure | None = None
```

Return at each comparison boundary. Catch `(TypeError, ValueError, WorkContractError)` only around shape/canonicalization. Make `_load_signing_secret` translate missing, unsafe permissions, too-short content, and unreadable key errors to a private `SigningKeyUnreadable`; map other `OSError` to `io_error`.

- [ ] **Step 4: Update boolean callers explicitly**

```python
verification = verify_work_contract(
    signed_contract, secret=secret, hermes_home=hermes_home
)
if not verification.valid:
    raise WorkContractError(
        f"Work Contract verification failed: {verification.failure}"
    )
```

Do not rely on dataclass truthiness.

- [ ] **Step 5: Run the full intake unit suite**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py -q`

Expected: PASS.

- [ ] **Step 6: Commit typed verification**

```bash
git add hermes_cli/kanban_intake.py tests/hermes_cli/test_kanban_intake.py
git commit -m "fix: report safe work contract failure paths"
```

### Task 2: Persist and surface only the safe verifier path

**Files:**
- Modify: `hermes_cli/kanban_intake.py` (qualification/materialization failure path)
- Modify: `hermes_cli/kanban_db.py` (`finish_qualification_intake_run` callers/events)
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Test: `tests/hermes_cli/test_kanban_intake.py`
- Test: `tests/plugins/test_kanban_dashboard_plugin.py`

**Interfaces:**
- Consumes: `WorkContractVerification.failure`, intake ID, and run ID.
- Produces: `work_contract_verification_failed` event and bounded status/detail field `failure_path`.

- [ ] **Step 1: Write failing redaction tests**

Use sentinel strings for raw payload, key, signature, digest, and canonical JSON. Trigger each failure and serialize the resulting exception, intake run, event, API status, and logs captured by pytest. Assert the safe path occurs and every sentinel is absent.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py tests/plugins/test_kanban_dashboard_plugin.py -k 'failure_path or verification_failed' -q`

Expected: FAIL because callers collapse paths into “signature is invalid.”

- [ ] **Step 3: Carry the safe path through the existing run/event identity**

```python
append_qualification_intake_event(
    conn,
    intake_id=intake_id,
    run_id=run_id,
    kind="work_contract_verification_failed",
    payload={"failure_path": verification.failure},
)
```

Store the bounded error as `work_contract:<failure_path>`; do not include an exception `repr` from cryptographic or filesystem operations.

- [ ] **Step 4: Expose the path in authenticated detail/status**

For the owning Work Inbox status and operator intake detail, derive `failure_path` from the latest safe event. Never return signed envelope columns from `work_contracts` or raw intake request content in this field.

- [ ] **Step 5: Run unit and API suites**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py tests/plugins/test_kanban_dashboard_plugin.py -q`

Expected: PASS.

- [ ] **Step 6: Commit safe propagation**

```bash
git add hermes_cli/kanban_intake.py hermes_cli/kanban_db.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_intake.py tests/plugins/test_kanban_dashboard_plugin.py
git commit -m "fix: surface safe intake verification reasons"
```

### Task 3: Enforce one active requalification intake

**Files:**
- Modify: `hermes_cli/kanban_intake.py` (`existing_requalification_intake`, `submit_requalification`)
- Modify: `hermes_cli/kanban_db.py` (qualification schema migration/index)
- Test: `tests/hermes_cli/test_kanban_intake.py`
- Test: `tests/hermes_cli/test_kanban_db.py`

**Interfaces:**
- Consumes: JSON intake keys `kind == "task_requalification"` and `target_task_id`.
- Produces: unique active-target constraint and idempotent concurrent submission result.

- [ ] **Step 1: Write failing lifecycle and concurrency tests**

Assert `existing_requalification_intake` treats `pending`, `running`, `needs_clarification`, and `attention_required` as active. Use two SQLite connections and a barrier to submit the same target concurrently; assert one row and one returned intake ID.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py tests/hermes_cli/test_kanban_db.py -k 'requalification and (active or concurrent)' -q`

Expected: FAIL because the query currently treats only `pending` as active and no target index exists.

- [ ] **Step 3: Reconcile legacy duplicates append-only during migration**

For each duplicated active target, keep the newest `(created_at, id)` active. For every older row, insert a `qualification_intake_decisions` rejection with actor `hermes-migration` and reason `superseded by active requalification intake <newest-id>`, then update that row to `rejected`. Preserve all runs/events/raw requests.

- [ ] **Step 4: Add the partial unique JSON1 index**

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_requalification_one_active_target
ON qualification_intake(json_extract(raw_request, '$.target_task_id'))
WHERE json_valid(raw_request)
  AND json_extract(raw_request, '$.kind') = 'task_requalification'
  AND status IN ('pending', 'running', 'needs_clarification', 'attention_required');
```

Update the Python preflight to the same four statuses. Catch only the index's `sqlite3.IntegrityError`, then return the winning active row; re-raise unrelated integrity failures.

- [ ] **Step 5: Run intake and migration tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_intake.py tests/hermes_cli/test_kanban_db.py -q`

Expected: PASS.

- [ ] **Step 6: Commit race-safe deduplication**

```bash
git add hermes_cli/kanban_intake.py hermes_cli/kanban_db.py tests/hermes_cli/test_kanban_intake.py tests/hermes_cli/test_kanban_db.py
git commit -m "fix: deduplicate active requalification intakes"
```

### Task 4: Bound total qualification attempts and manual retry

**Files:**
- Modify: `hermes_cli/kanban_db.py` (`retry_qualification_intake`, attempt accounting)
- Modify: `hermes_cli/kanban_intake.py` (board policy accessor)
- Modify: `plugins/kanban/dashboard/plugin_api.py` (`POST /work-inbox` retry and status)
- Test: `tests/hermes_cli/test_kanban_db.py`
- Test: `tests/plugins/test_kanban_dashboard_plugin.py`

**Interfaces:**
- Consumes: board `qualification.max_total_attempts` and count of all `qualification_intake_runs` for the intake.
- Produces: `qualification_retry_state(conn, intake_id, max_total_attempts) -> RetryState` and a budget-enforcing retry CAS.

- [ ] **Step 1: Write failing budget tests**

Set `max_total_attempts: 3`. Prove attempts one through three may run, an `attention_required` intake with three historical runs advertises no retry, retry refuses with `attempt_budget_exhausted`, and manual retry does not delete/reset any run.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py -k 'attempt_budget or intake_retry' -q`

Expected: FAIL because retry only checks status/current run.

- [ ] **Step 3: Implement one budget calculation used by claim, status, and retry**

```python
@dataclass(frozen=True)
class RetryState:
    attempts_used: int
    attempts_limit: int
    allowed: bool
    reason: str | None
```

Count every historical run, not only selected failure outcomes. `claim_qualification_intake` and `retry_qualification_intake` must both refuse when used reaches the limit so a stale client cannot bypass the status response.

- [ ] **Step 4: Add authenticated action metadata**

For the original Work Inbox principal and `attention_required` with budget remaining, return:

```json
{"actions":[{"id":"retry","method":"POST","target":"work-inbox"}]}
```

Return `actions: []`, `attempts_used`, and `attempts_limit` otherwise. The existing `WorkInboxRetryBody` path remains the mutation endpoint.

- [ ] **Step 5: Run DB/API tests**

Run: `scripts/run_tests.sh tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py -q`

Expected: PASS.

- [ ] **Step 6: Commit bounded retry**

```bash
git add hermes_cli/kanban_db.py hermes_cli/kanban_intake.py plugins/kanban/dashboard/plugin_api.py tests/hermes_cli/test_kanban_db.py tests/plugins/test_kanban_dashboard_plugin.py
git commit -m "fix: bound qualification retry attempts"
```

### Task 5: Add dashboard and CLI inspect/retry surfaces

**Files:**
- Modify: `plugins/kanban/dashboard/dist/index.js`
- Modify: `plugins/kanban/dashboard/dist/style.css`
- Modify: `plugins/kanban/dashboard/plugin_api.py`
- Modify: `hermes_cli/kanban.py`
- Modify: `tests/plugins/kanban_dashboard_client_contract.js`
- Modify: `tests/plugins/test_kanban_dashboard_plugin.py`
- Modify: `tests/hermes_cli/test_kanban_cli.py`

**Interfaces:**
- Consumes: authenticated status `actions`, `failure_path`, `attempts_used`, and `attempts_limit`.
- Produces: dashboard Retry action and CLI `hermes kanban intake show <id>` / `hermes kanban intake retry <id>`.

- [ ] **Step 1: Write failing UI/client/CLI behavior tests**

Assert Retry renders only from an `actions[id=retry]` response, posts the existing `WorkInboxRetryBody`, disables while pending, refreshes status after 202, and shows exhausted budget without a button. CLI show must print safe path/budget; CLI retry must exit nonzero for owner/status/budget refusal.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_cli.py -k 'intake and retry' -q`

Expected: FAIL because no action UI/CLI subcommands exist.

- [ ] **Step 3: Implement the dashboard action from server capability**

Do not infer permission from status text. Find the action object by ID, render one button, post `{kind:"retry", intake_id}`, and replace the view with the refreshed authenticated status.

- [ ] **Step 4: Implement operator CLI commands**

`intake show` reads durable record, latest run/event, safe failure path, and retry state. `intake retry` calls the same budget-aware DB function, appends the existing retry event, wakes the qualifier, and prints only intake ID plus new state.

- [ ] **Step 5: Run dashboard, API, and CLI suites**

Run: `scripts/run_tests.sh tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_cli.py -q`

Expected: PASS.

- [ ] **Step 6: Commit recovery surfaces**

```bash
git add plugins/kanban/dashboard/dist/index.js plugins/kanban/dashboard/dist/style.css plugins/kanban/dashboard/plugin_api.py hermes_cli/kanban.py tests/plugins/kanban_dashboard_client_contract.js tests/plugins/test_kanban_dashboard_plugin.py tests/hermes_cli/test_kanban_cli.py
git commit -m "feat: expose bounded intake recovery actions"
```
