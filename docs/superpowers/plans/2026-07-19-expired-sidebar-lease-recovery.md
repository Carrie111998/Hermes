# Expired Sidebar Lease Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover expired native-sidebar leases in one claim call while preserving their exact bound Codex thread IDs and exposing them as actionable work to the status-first broker.

**Architecture:** Keep recovery inside the existing SQLite `BEGIN IMMEDIATE` claim transaction: expire leased rows first, then select and lease due retry/pending rows. Keep raw durable counts unchanged, but make `sidebar_delivery_status(now)` reclassify expired leases as retry/actionable for the broker preflight.

**Tech Stack:** Python 3.12, SQLite, pytest, Ruff, ty, Session Bridge MCP, Codex desktop automations.

---

### Task 1: Prove first-call exact-ID lease recovery

**Files:**
- Modify: `tests/session_bridge/test_store.py`
- Modify: `session_bridge/store.py:3791-3892`

- [ ] **Step 1: Write the failing first-call regression**

Replace the existing two-call expired-lease test with a regression that binds an
exact native ID and expects the first claim at expiry to return that same job:

```python
def test_expired_sidebar_lease_is_reclaimed_by_first_claim_with_bound_thread(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("expired-token", "recovered-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="expired-bound", eligible_at=20.0)
    store.enqueue_sidebar_job(candidate)
    first = store.claim_sidebar_jobs(now=100.0, limit=1)[0]
    store.bind_sidebar_thread(
        lease_token=first["lease_token"],
        codex_thread_id="codex-bound-thread",
        now=150.0,
    )

    recovered = store.claim_sidebar_jobs(now=400.0, limit=1)[0]

    assert recovered["source_session_id"] == candidate.source_session_id
    assert recovered["lease_token"] == "recovered-token"
    assert recovered["codex_thread_id"] == "codex-bound-thread"
    assert recovered["state"] == SidebarJobState.LEASED.value
    with pytest.raises(ValueError, match="lease token"):
        store.commit_sidebar_job(
            lease_token=first["lease_token"],
            codex_thread_id="codex-bound-thread",
            now=400.0,
        )
```

- [ ] **Step 2: Run the regression and verify RED**

Run:

```powershell
uv run --no-sync pytest tests/session_bridge/test_store.py::test_expired_sidebar_lease_is_reclaimed_by_first_claim_with_bound_thread -q
```

Expected: FAIL because the first claim call returns no row.

- [ ] **Step 3: Move expired-lease recovery before due selection**

In `SessionBridgeStore.claim_sidebar_jobs`, execute the existing expired-lease
`UPDATE` before the due-row `SELECT`, preserving the same transaction, retry state,
cleared lease fields, exact `codex_thread_id`, and retry-first ordering:

```python
def _write(conn):
    conn.execute(
        """UPDATE session_sidebar_jobs
           SET state = ?, next_attempt_at = ?, lease_digest = NULL,
               lease_expires_at = NULL, error_code = NULL, updated_at = ?
           WHERE state = ? AND lease_expires_at <= ?""",
        (
            SidebarJobState.RETRY.value,
            claim_time,
            claim_time,
            SidebarJobState.LEASED.value,
            claim_time,
        ),
    )
    due = conn.execute(
        """SELECT * FROM session_sidebar_jobs
           WHERE state IN (?, ?) AND next_attempt_at <= ?
           ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END,
                    eligible_at, id
           LIMIT ?""",
        (
            SidebarJobState.PENDING.value,
            SidebarJobState.RETRY.value,
            claim_time,
            SidebarJobState.RETRY.value,
            _SIDEBAR_CLAIM_SCAN_LIMIT,
        ),
    ).fetchall()
```

- [ ] **Step 4: Run focused store recovery tests and verify GREEN**

Run:

```powershell
uv run --no-sync pytest tests/session_bridge/test_store.py -k "sidebar and (expired or claim or bind)" -q
```

Expected: all selected tests pass.

### Task 2: Expose expired leases as actionable status

**Files:**
- Modify: `tests/session_bridge/test_store.py`
- Modify: `session_bridge/store.py:4436-4525`

- [ ] **Step 1: Write the failing status regression**

```python
def test_sidebar_delivery_status_reclassifies_expired_lease_as_retry(db) -> None:
    store = SessionBridgeStore(
        db,
        sidebar_token_factory=_token_factory("status-token"),
        sidebar_jitter=lambda _bound: 0.0,
    )
    candidate = _sidebar_candidate(db, native_id="status-expired")
    store.enqueue_sidebar_job(candidate)
    store.claim_sidebar_jobs(now=100.0, limit=1)

    status = store.sidebar_delivery_status(now=400.0)

    assert status["counts"]["sidebar_leased"] == 0
    assert status["counts"]["sidebar_retry"] == 1
```

Also assert an unexpired lease at `now=399.999` remains leased and not retry.

- [ ] **Step 2: Run the status regression and verify RED**

Run:

```powershell
uv run --no-sync pytest tests/session_bridge/test_store.py::test_sidebar_delivery_status_reclassifies_expired_lease_as_retry -q
```

Expected: FAIL with leased `1` and retry `0`.

- [ ] **Step 3: Add read-only effective status classification**

Inside `sidebar_delivery_status`, query expired leased rows using `status_time`, then
shift only the returned status counts:

```python
expired_row = conn.execute(
    """SELECT COUNT(*) AS job_count
         FROM session_sidebar_jobs
        WHERE state = ? AND lease_expires_at <= ?""",
    (SidebarJobState.LEASED.value, status_time),
).fetchone()
expired_leases = int(expired_row["job_count"])
counts[SidebarJobState.LEASED.value] -= expired_leases
counts[SidebarJobState.RETRY.value] += expired_leases
```

Keep durable database state unchanged until `claim_sidebar_jobs` executes.

- [ ] **Step 4: Run focused status and MCP tests**

Run:

```powershell
uv run --no-sync pytest tests/session_bridge/test_store.py -k "sidebar_delivery_status or sidebar_counts" -q
uv run --no-sync pytest tests/session_bridge/test_mcp_server.py -k "session_status or sidebar_pending" -q
```

Expected: all selected tests pass.

### Task 3: Verify, deploy, and reconcile production

**Files:**
- Verify: `session_bridge/store.py`
- Verify: `tests/session_bridge/test_store.py`
- Deploy through: canonical Session Bridge launcher
- Update: Codex automation `session-sidebar-sync`

- [ ] **Step 1: Run affected suites and static checks**

```powershell
uv run --no-sync pytest tests/session_bridge/test_store.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_coordinator.py -q
uv run --no-sync ruff check session_bridge/store.py tests/session_bridge/test_store.py
uv run --no-sync ty check session_bridge/store.py
git diff --check
```

Expected: all tests and checks pass.

- [ ] **Step 2: Commit the tested fix**

```powershell
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "fix: reclaim expired sidebar leases atomically"
```

- [ ] **Step 3: Deploy and prove service health**

Restart through the canonical Session Bridge launcher, then require the service
health endpoint and `session_status` to report the watcher running with no provider
degradation.

- [ ] **Step 4: Reconcile the two production rows**

Run one bounded broker batch. Each returned row must include its existing
`recovered_thread_id`; read that exact ID, authenticate the marker and local project,
bind idempotently, rename, and commit. Do not call marker search or create a task.

- [ ] **Step 5: Resume and verify the worker**

Set `session-sidebar-sync` active with its established one-minute cadence. Verify an
empty cycle with pending, leased, retry, and failed all zero, unique visible source,
bridge, idempotency, and Codex thread identities, and no replacement native tasks.

- [ ] **Step 6: Capture durable memory checkpoints**

Search before writing, then record the shipped fix and production evidence in the
`session-bridge` MemPalace wing and the corresponding GBrain project timeline when
its tools are available.
