# Session Sidebar `notLoaded` Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover an exact failed, already-bound Codex registration task whose authenticated completed turn reports `notLoaded`, without creating a replacement, and prevent the same state from blocking future sidebar delivery.

**Architecture:** Extend the installed worker contract with a narrow authenticated-quiescence predicate for `notLoaded`. Add a separate compare-and-swap store operation and CLI command that can requeue only an exact failed bound `native_task_not_indexed` row while preserving its task ID and create reservation. Deploy the asset, recover the live row, run one worker cycle, and verify the queue advances without duplicate creation.

**Tech Stack:** Python 3.12, SQLite, argparse, pytest through `scripts/run_tests.sh`, Markdown Codex skill assets, Codex desktop native task tools.

---

## File map

- Modify `session_bridge/assets/session-sidebar-sync/SKILL.md`: define the authenticated completed-registration `notLoaded` predicate and use it at both recovered-task and newly-created-task read gates.
- Modify `tests/session_bridge/test_sidebar_skill.py`: lock the quiescence predicate and fail-closed exclusions into the generated skill contract.
- Modify `session_bridge/store.py`: add the exact-bound operator retry transaction.
- Modify `tests/session_bridge/test_store.py`: prove successful preservation plus stale, unsafe, and replay rejection.
- Modify `session_bridge/cli.py`: expose the guarded retry through the backend protocol, production backend, parser, dispatcher, and sanitized public result.
- Modify `tests/session_bridge/test_cli.py`: prove parser authority, dispatch, output validation, and production-store integration.
- Modify the installed `C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md` only through the existing `install-sidebar-skill` command during rollout.

### Task 1: Authenticate completed `notLoaded` registration tasks

**Files:**
- Modify: `tests/session_bridge/test_sidebar_skill.py`
- Modify: `session_bridge/assets/session-sidebar-sync/SKILL.md`

- [ ] **Step 1: Write the failing skill-contract test**

Add:

```python
def test_sidebar_skill_accepts_only_authenticated_completed_notloaded_tasks() -> None:
    skill = (ASSET / "SKILL.md").read_text(encoding="utf-8")

    assert "authenticated quiescent registration" in skill
    assert "literal `idle`" in skill
    assert "top-level status is `notLoaded`" in skill
    assert "at least one returned turn" in skill
    assert "every returned turn has status `completed`" in skill
    assert "no active turn, approval request, user-input request, or system error" in skill
    assert "Never treat `notLoaded` as globally equivalent to `idle`" in skill
    assert "missing turns" in skill
    assert "incomplete turn" in skill
    assert "exact signed marker" in skill
```

Update the existing new-task indexing assertion from the literal phrase
`status is \`idle\`` to the new phrase
`authenticated quiescent registration`.

- [ ] **Step 2: Run the skill test and verify RED**

Run:

```powershell
$env:HERMES_PYTHON='/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe'
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py -k 'authenticated_completed_notloaded or waits_for_new_task_indexing' -v
```

Expected: FAIL because the asset does not define authenticated `notLoaded`
quiescence and still requires literal `idle`.

- [ ] **Step 3: Add the minimal worker-contract text**

Add one shared rule before the procedure:

```markdown
## Authenticated Quiescent Registration

A readable task is an authenticated quiescent registration only after its exact
thread ID, normalized local host, chosen project/cwd identity, and exact signed
marker have passed the applicable checks below, and either:

- its literal top-level status is `idle`; or
- its top-level status is `notLoaded`, at least one returned turn is present, every
  returned turn has status `completed`, and the response contains no active turn,
  approval request, user-input request, or system error.

Never treat `notLoaded` as globally equivalent to `idle`. Missing turns, an
incomplete turn, an active turn, an approval or user-input request, a system error,
or any identity/marker mismatch is not quiescent and must continue polling or fail
closed under the fixed mapping.
```

Replace the recovered and new-task gates so they proceed only after the shared
authenticated-quiescence rule passes. Preserve every existing exact-ID, local-host,
project/cwd, signed-marker, bind-before-mutation, and no-replacement requirement.

- [ ] **Step 4: Run the skill tests and verify GREEN**

Run the Task 1 command again.

Expected: selected tests PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add session_bridge/assets/session-sidebar-sync/SKILL.md tests/session_bridge/test_sidebar_skill.py
git commit -m "fix(session-bridge): accept authenticated notLoaded registrations"
```

### Task 2: Add the exact-bound store retry

**Files:**
- Modify: `tests/session_bridge/test_store.py`
- Modify: `session_bridge/store.py`

- [ ] **Step 1: Write the successful-preservation test**

Add a test that enqueues a candidate, leases it, reserves create intent, binds
`019f-bound-retry-thread`, fails it five times with
`native_task_not_indexed`, and calls:

```python
retried = store.retry_failed_bound_sidebar_job(
    job_id=failed["id"],
    source_session_id=candidate.source_session_id,
    codex_thread_id="019f-bound-retry-thread",
    expected_error_code="native_task_not_indexed",
    confirmation="PRESERVE_EXACT_BOUND_TASK",
    now=1_000.0,
)
```

Assert:

```python
assert retried["state"] == SidebarJobState.RETRY.value
assert retried["attempts"] == 0
assert retried["next_attempt_at"] == 1_000.0
assert retried["error_code"] is None
assert retried["codex_thread_id"] == "019f-bound-retry-thread"
assert store.get_sidebar_create_reservation(candidate.source_session_id) == reservation
claimed = store.claim_sidebar_jobs(now=1_000.0, limit=1)[0]
assert claimed["codex_thread_id"] == "019f-bound-retry-thread"
```

- [ ] **Step 2: Run the successful test and verify RED**

Run:

```powershell
$env:HERMES_PYTHON='/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe'
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/session_bridge/test_store.py -k 'bound_sidebar_operator_retry' -v
```

Expected: FAIL with `AttributeError` because
`retry_failed_bound_sidebar_job` does not exist.

- [ ] **Step 3: Implement the minimal compare-and-swap operation**

Add this public method beside `retry_failed_sidebar_job`:

```python
def retry_failed_bound_sidebar_job(
    self,
    *,
    job_id: str,
    source_session_id: str,
    codex_thread_id: str,
    expected_error_code: str,
    confirmation: str,
    now: float,
) -> dict[str, Any]:
```

Normalize all text with `_exact_nonempty_text`. Require the confirmation to equal
`PRESERVE_EXACT_BOUND_TASK` and the error to equal
`native_task_not_indexed`. Derive the canonical idempotency key, bridge ID, and job
ID from the source session and require the supplied job ID to match.

Inside one `_execute_write` transaction:

1. Require a valid terminal-resolution ledger.
2. Select at most two rows for the source and require exactly one.
3. Require exact job, source, idempotency, bridge, bound task ID, failed state,
   expected error, no lease, no completion digest, no visible timestamp, and no
   terminal or precreate resolution.
4. Load and decode the source's create reservation. Require the same job, source,
   and bridge identity.
5. Run one guarded update:

```sql
UPDATE session_sidebar_jobs
   SET state = ?, attempts = 0, next_attempt_at = ?,
       lease_digest = NULL, lease_expires_at = NULL,
       error_code = NULL, updated_at = ?
 WHERE id = ? AND idempotency_key = ? AND bridge_id = ?
   AND source_session_id = ? AND codex_thread_id = ?
   AND state = ? AND error_code = ?
   AND lease_digest IS NULL AND lease_expires_at IS NULL
   AND completion_digest IS NULL AND visible_at IS NULL
```

Use `SidebarJobState.RETRY.value` as the new state and reject a row count other than
one with `ValueError("expected bound sidebar failure does not match")`.

- [ ] **Step 4: Verify GREEN**

Run the Task 2 test command again.

Expected: the successful bound retry test PASS.

- [ ] **Step 5: Add fail-closed parameterized tests**

Parameterize mutations for:

- wrong job ID;
- wrong source session ID;
- wrong Codex task ID;
- wrong error code;
- wrong confirmation;
- missing or conflicting create reservation;
- active lease;
- completion digest;
- visible timestamp;
- terminal resolution history;
- precreate resolution history;
- duplicate source sibling;
- repeated retry invocation.

For each case assert `ValueError` and assert the full job row and reservation remain
unchanged.

- [ ] **Step 6: Run all store operator-retry tests**

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/session_bridge/test_store.py -k 'sidebar_operator_retry or bound_sidebar_operator_retry or terminal_resolution_rejects_any_job' -v
```

Expected: all selected tests PASS.

- [ ] **Step 7: Commit Task 2**

```powershell
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): retry exact bound sidebar task"
```

### Task 3: Expose the guarded operator CLI

**Files:**
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `session_bridge/cli.py`

- [ ] **Step 1: Write CLI dispatch and sanitization tests**

Extend `FakeBackend` with:

```python
sidebar_bound_retry_payload: dict[str, Any] = field(
    default_factory=lambda: {
        "status": "requeued",
        "job_id": "sidebar-job:" + "a" * 64,
        "codex_thread_id": "019f-bound-retry-thread",
        "error_code": "native_task_not_indexed",
        "state": "sidebar_retry",
    }
)

def sidebar_retry_bound(
    self,
    *,
    job_id: str,
    source_session_id: str,
    codex_thread_id: str,
    expected_error_code: str,
    confirmation: str,
) -> dict[str, Any]:
    self.calls.append((
        "sidebar_retry_bound",
        job_id,
        source_session_id,
        codex_thread_id,
        expected_error_code,
        confirmation,
    ))
    return dict(self.sidebar_bound_retry_payload)
```

Invoke `sidebar-retry-bound` with all five exact authority arguments. Assert exit
zero, one backend call, and public JSON equal to:

```python
{
    "status": "requeued",
    "job_id": job_id,
    "codex_thread_id": thread_id,
    "state": "sidebar_retry",
}
```

Add parser-rejection cases for every missing or invalid argument and a public-result
test that rejects any state, error, job ID, or task ID mismatch.

- [ ] **Step 2: Run the CLI tests and verify RED**

Run:

```powershell
$env:HERMES_PYTHON='/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe'
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/session_bridge/test_cli.py -k 'sidebar_retry_bound' -v
```

Expected: FAIL because the parser and backend method do not exist.

- [ ] **Step 3: Implement parser, dispatch, and public validation**

Add `sidebar_retry_bound` to `_Backend`. Add the parser:

```python
sidebar_retry_bound = commands.add_parser(
    "sidebar-retry-bound",
    help="requeue one exact failed bound sidebar task without replacement",
)
sidebar_retry_bound.add_argument("--job-id", type=_sidebar_terminal_job_id, required=True)
sidebar_retry_bound.add_argument("--source-session-id", required=True)
sidebar_retry_bound.add_argument(
    "--codex-thread-id", type=_sidebar_terminal_thread_id, required=True
)
sidebar_retry_bound.add_argument(
    "--expected-error-code",
    choices=("native_task_not_indexed",),
    required=True,
)
sidebar_retry_bound.add_argument(
    "--confirm",
    choices=("PRESERVE_EXACT_BOUND_TASK",),
    required=True,
)
```

Dispatch it before the terminal-resolution commands, pass every argument unchanged,
validate through `_public_sidebar_bound_retry_result`, emit JSON, and return
`EXIT_OK`.

The public validator must require:

- `status == "requeued"`;
- `state == SidebarJobState.RETRY.value`;
- `error_code == "native_task_not_indexed"`;
- canonical job-ID and Codex-task-ID syntax.

Return only `status`, `state`, `job_id`, and `codex_thread_id`.

- [ ] **Step 4: Implement the production backend method**

Add:

```python
def sidebar_retry_bound(
    self,
    *,
    job_id: str,
    source_session_id: str,
    codex_thread_id: str,
    expected_error_code: str,
    confirmation: str,
) -> Mapping[str, Any]:
```

Validate the fixed error and confirmation, call
`store.retry_failed_bound_sidebar_job(..., now=time.time())`, translate
`TypeError`/`ValueError` to
`RolloutGateBlocked("sidebar_bound_retry_snapshot_mismatch")`, and return:

```python
{
    "status": "requeued",
    "job_id": result["id"],
    "codex_thread_id": result["codex_thread_id"],
    "error_code": "native_task_not_indexed",
    "state": result["state"],
}
```

- [ ] **Step 5: Add the production integration test**

Create a real store row with reservation, binding, and exhausted
`native_task_not_indexed`; call `ProductionBackend.sidebar_retry_bound`; assert the
exact row and reservation are preserved and a replay raises
`RolloutGateBlocked("sidebar_bound_retry_snapshot_mismatch")`.

- [ ] **Step 6: Run CLI tests and verify GREEN**

Run the Task 3 command again, then:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh tests/session_bridge/test_cli.py -k 'sidebar_retry_bound or sidebar_terminal_acknowledgement' -v
```

Expected: all selected tests PASS.

- [ ] **Step 7: Commit Task 3**

```powershell
git add session_bridge/cli.py tests/session_bridge/test_cli.py
git commit -m "feat(session-bridge): expose bound sidebar recovery"
```

### Task 4: Run regression verification and install the worker asset

**Files:**
- Verify: `session_bridge/assets/session-sidebar-sync/SKILL.md`
- Install: `C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md`

- [ ] **Step 1: Run the focused Session Bridge suite**

```powershell
$env:HERMES_PYTHON='/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe'
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh `
  tests/session_bridge/test_sidebar_skill.py `
  tests/session_bridge/test_store.py `
  tests/session_bridge/test_cli.py `
  tests/session_bridge/test_end_to_end.py `
  -j 4
```

Expected: all files PASS with no warnings or errors.

- [ ] **Step 2: Run static checks**

```powershell
git diff --check
& 'C:\Program Files\Git\bin\bash.exe' scripts/run_tests.sh `
  tests/session_bridge/test_sidebar_skill.py `
  tests/session_bridge/test_store.py `
  tests/session_bridge/test_cli.py `
  -k 'notloaded or bound_sidebar or sidebar_retry_bound'
```

Expected: clean diff and selected tests PASS.

- [ ] **Step 3: Install from the repair worktree**

Use the worktree's Python package entry point:

```powershell
& 'C:\Users\diego\.hermes\agent-src\.venv\Scripts\python.exe' -m session_bridge.cli install-sidebar-skill
```

Run it with `PYTHONPATH` pointing to this repair worktree so the installed asset is
the just-tested version.

- [ ] **Step 4: Verify installed bytes**

Compute SHA-256 for the source and installed `SKILL.md` and require equality.

- [ ] **Step 5: Commit any verification-only documentation changes**

No commit is expected. Require `git status --short` to be clean.

### Task 5: Recover the live blocker without replacement

**Files:**
- Live Session Bridge state through the supported CLI
- Existing Codex task `019f8bed-2a9f-7353-a841-551fc1c8b68e`

- [ ] **Step 1: Capture the pre-recovery snapshot**

Read sanitized Session Bridge status and the exact bound Codex task. Require:

- failed job
  `sidebar-job:4d1adae2b78f3e0cd7697c21f5bd03851f5bf576132c153e38987e44ac101ba3`;
- source `claude:2a23388a-5d72-4e0d-bbad-5e655ef4c8a3`;
- task `019f8bed-2a9f-7353-a841-551fc1c8b68e`;
- error `native_task_not_indexed`;
- exact signed marker and completed registration turn;
- no active turn or system error.

- [ ] **Step 2: Invoke the guarded retry once**

```powershell
& 'C:\Users\diego\.hermes\agent-src\.venv\Scripts\python.exe' -m session_bridge.cli `
  sidebar-retry-bound `
  --job-id sidebar-job:4d1adae2b78f3e0cd7697c21f5bd03851f5bf576132c153e38987e44ac101ba3 `
  --source-session-id claude:2a23388a-5d72-4e0d-bbad-5e655ef4c8a3 `
  --codex-thread-id 019f8bed-2a9f-7353-a841-551fc1c8b68e `
  --expected-error-code native_task_not_indexed `
  --confirm PRESERVE_EXACT_BOUND_TASK
```

Require `status=requeued`, `state=sidebar_retry`, and the same exact task ID.

- [ ] **Step 3: Run the sidebar skill exactly once**

Invoke `$session-sidebar-sync` once from the broker task. It must lease the recovered
row, read the exact existing task directly, accept only the authenticated completed
`notLoaded` state, rename it, and commit it. It must not call `create_thread`.

- [ ] **Step 4: Verify convergence**

Require:

- the exact row is visible/completed;
- `blocking_failed_count` decreases and `sidebar_failed` is absent from execution
  blockers;
- the exact task ID remains unchanged;
- the task title is the expected `[Claude]` title;
- no second task contains the exact signed marker.

- [ ] **Step 5: Run one bounded subsequent delivery**

Invoke the skill once more only if status reports actionable pending/retry work.
Require the next job to settle without recreating the recovered task.

- [ ] **Step 6: Resume the heartbeat**

Restore automation `session-sidebar-sync-worker` to `ACTIVE` with its existing
three-minute cadence, prompt, target task, and failed-runs-only notification policy.

### Task 6: Final verification and durable incident record

**Files:**
- Verify the repair worktree and live Session Bridge status
- Record to MemPalace wing `session-bridge`
- Update the relevant GBrain page or timeline after searching for an existing page

- [ ] **Step 1: Run verification-before-completion checks**

Run the focused test suite again, `git diff --check`, `git status --short --branch`,
and the sanitized live sidebar status. Do not claim success unless all outputs are
fresh and passing.

- [ ] **Step 2: Record the incident**

Write one MemPalace drawer containing the original blocker, root cause, exact task
preservation, implementation commits, test evidence, live recovery evidence, and
automation state. Search GBrain first, then update the existing Session Bridge page
or create one if absent.

- [ ] **Step 3: Report completion**

Report the preserved task ID, blocker clearance, pending queue movement, test
counts, commits, worker status, and the separate non-goal that transcript
translation remains unresolved.
