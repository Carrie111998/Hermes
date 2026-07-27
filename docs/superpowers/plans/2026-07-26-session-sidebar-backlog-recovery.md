# Session Sidebar Backlog Recovery Implementation Plan

> **For Codex:** Execute this plan inline and in order. The repository forbids
> subagents. Use test-driven development for every behavior change and run the
> verification matrix before claiming completion.

**Goal:** Make every eligible Claude Code Desktop session appear promptly in one
exact Codex task, hydrate every eligible legacy placeholder task in place with a
readable summary and the last five messages, and drain the accumulated backlog
without recreating tasks or reviving the disruptive sidebar heartbeat.

**Architecture:** Keep the existing durable SQLite job and hydration ledgers as the
source of truth. Replace FIFO-only pending selection with a transactionally
persisted 3-fresh-to-1-oldest scheduler while preserving absolute retry priority.
Add a guarded inventory-and-seed operation that authenticates exact existing Codex
tasks and seeds hydration only for legacy placeholders. Move both hydration and
registration delivery into one service-owned, serialized recovery worker that runs
independently of provider scans and always processes hydration before registration.
Continue using reserve-before-send and post-send marker verification so an
ambiguous native operation is reconciled, never repeated.

**Tech stack:** Python 3.12, asyncio/threading, SQLite, Codex app-server JSON-RPC,
pytest, uv.

---

## Invariants to preserve throughout

- A source session may have only one `session_sidebar_jobs` row and one exact
  `codex_thread_id`.
- Recovery may append a readable hydration turn to that exact task; it may not
  create a replacement task.
- Retry jobs outrank all new pending jobs.
- A reserved or ambiguous hydration send is reconciled by reading the exact task
  and matching its authenticated marker; it is never sent a second time.
- The recovery worker is internal to the Hermes service. Do not recreate the
  `session-sidebar-sync-worker` automation.
- Public status returns fixed error codes and redacted task identifiers only.

## Task 1: Add a durable 3-fresh-to-1-oldest sidebar claim scheduler

**Files:**

- Modify: `session_bridge/store.py`
- Test: `tests/session_bridge/test_store.py`

### Step 1: Write failing store tests

Add focused tests beside the existing `claim_sidebar_jobs` coverage:

```python
def test_claim_sidebar_jobs_prioritizes_retry_before_pending(store, now):
    retry = seed_sidebar_job(store, source_id="claude:retry", eligible_at=now - 100)
    newest = seed_sidebar_job(store, source_id="claude:newest", eligible_at=now)
    fail_sidebar_job_into_retry(store, retry, now=now)

    claim = store.claim_sidebar_jobs(now=now, limit=1)

    assert claim[0]["id"] == retry["id"]


def test_claim_sidebar_jobs_uses_three_fresh_then_one_oldest(store, now):
    oldest = seed_sidebar_job(store, source_id="claude:oldest", eligible_at=now - 100)
    fresh = [
        seed_sidebar_job(
            store,
            source_id=f"claude:fresh-{index}",
            eligible_at=now - index,
        )
        for index in range(1, 5)
    ]

    claimed_ids = [
        claim_and_return_to_pending(store, now=now + index)["id"]
        for index in range(4)
    ]

    assert claimed_ids == [
        fresh[0]["id"],
        fresh[1]["id"],
        fresh[2]["id"],
        oldest["id"],
    ]


def test_sidebar_claim_lane_survives_store_restart(db_path, now):
    store = open_store(db_path)
    seed_four_pending_jobs(store, now=now)
    claim_and_complete(store, now=now)
    claim_and_complete(store, now=now + 1)
    store.close()

    reopened = open_store(db_path)
    third = reopened.claim_sidebar_jobs(now=now + 2, limit=1)[0]
    fourth = claim_complete_and_claim_again(reopened, third, now=now + 3)

    assert third["source_session_id"] == "claude:fresh-3"
    assert fourth["source_session_id"] == "claude:oldest"
```

Also test deterministic `eligible_at, id` tie-breaking and that a transaction rollback
does not advance the lane counter.

Run:

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge\test_store.py -k "sidebar_claim and (fresh or retry or lane)" -q
```

Expected: FAIL because pending jobs are currently strictly oldest-first and no lane
state is persisted.

### Step 2: Implement the scheduler transactionally

In `session_bridge/store.py`:

```python
_SIDEBAR_PENDING_LANE_STATE_KEY = "session-bridge:sidebar:pending-lane:v1"
_SIDEBAR_FRESH_BURST = 3
```

Within the same `_execute_write` transaction used by `claim_sidebar_jobs`:

1. Recover expired leases.
2. Select a due retry ordered by `next_attempt_at, eligible_at, id`; if one exists,
   claim it without changing pending-lane state.
3. Otherwise decode a persisted counter constrained to `0..2`.
4. For counters `0..2`, select the newest due pending job ordered by
   `eligible_at DESC, id DESC`.
5. On the fourth pending claim, select the oldest due pending job ordered by
   `eligible_at, id`.
6. Persist the next counter only after the row update succeeds.

Keep the existing provider validation and blocker checks. Implement `limit > 1` by
repeating this selection inside the same transaction so ordering remains exact.

### Step 3: Run the store tests

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge\test_store.py -k "sidebar" -q
```

Expected: PASS.

### Step 4: Commit

```powershell
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "fix(session-bridge): prevent sidebar backlog starvation"
```

## Task 2: Add exact native classification for legacy placeholder tasks

**Files:**

- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/sidebar_executor.py`
- Test: `tests/session_bridge/test_sidebar.py`
- Test: `tests/session_bridge/test_sidebar_executor.py`

### Step 1: Write failing classifier tests

Add a pure classifier that recognizes only the two authenticated initial prompt
forms already emitted by the bridge:

```python
class SidebarInitialPromptKind(StrEnum):
    LEGACY_PLACEHOLDER = "legacy_placeholder"
    READABLE_REGISTRATION = "readable_registration"
    UNRELATED = "unrelated"


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (legacy_registration_prompt, SidebarInitialPromptKind.LEGACY_PLACEHOLDER),
        (readable_registration_prompt, SidebarInitialPromptKind.READABLE_REGISTRATION),
        ("normal user task", SidebarInitialPromptKind.UNRELATED),
    ],
)
def test_classify_sidebar_initial_prompt(prompt, expected, marker_secret):
    assert classify_sidebar_initial_prompt(prompt, marker_secret) is expected
```

In `test_sidebar_executor.py`, test that `CodexAppServerSidebarDelivery` reads the
exact task, returns its first user prompt, resumes only when required, and rejects
a mismatched task ID or malformed turn history.

Run:

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_sidebar.py tests\session_bridge/test_sidebar_executor.py -k "initial_prompt or classify" -q
```

Expected: FAIL because the public classifier and exact first-prompt read do not
exist.

### Step 2: Implement exact classification and read

Expose a narrow helper in `sidebar.py` that validates the signed bridge marker and
uses the existing registration prompt validators. Do not classify by title,
timestamp, or text prefix alone.

Extend `NativeSidebarDelivery` and `CodexAppServerSidebarDelivery` with:

```python
def read_thread_initial_prompt(
    self,
    *,
    thread_id: str,
    deadline: float,
) -> str:
    thread = self._read_or_resume_thread(thread_id, deadline=deadline)
    return _exact_first_user_text(thread, expected_thread_id=thread_id)
```

The method is read-only and must never create, rename, or start a turn.

### Step 3: Run tests and commit

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_sidebar.py tests\session_bridge/test_sidebar_executor.py -k "initial_prompt or classify" -q
git add session_bridge/sidebar.py session_bridge/sidebar_executor.py tests/session_bridge/test_sidebar.py tests/session_bridge/test_sidebar_executor.py
git commit -m "feat(session-bridge): authenticate legacy sidebar placeholders"
```

## Task 3: Add guarded bulk hydration inventory and seeding

**Files:**

- Modify: `session_bridge/store.py`
- Modify: `session_bridge/cli.py`
- Test: `tests/session_bridge/test_store.py`
- Test: `tests/session_bridge/test_cli.py`

### Step 1: Write failing inventory and CLI tests

Store tests must prove the candidate query returns only visible Claude jobs inside
the requested age window, with intact lineage and no existing hydration job.

CLI tests must use a fake exact native reader and cover:

- dry-run is the default and performs no writes;
- readable registrations are reported as `already_readable` and not seeded;
- legacy placeholders are reported as `eligible`;
- unrelated or identity-mismatched tasks block the apply operation;
- apply requires exactly `HYDRATE_ALL_EXACT_EXISTING_TASKS`;
- repeated apply is idempotent and does not create a second hydration job.

Representative interface:

```python
result = backend.sidebar_hydration_seed_backfill(
    days=30,
    apply=False,
    confirmation=None,
)
assert result == {
    "mode": "dry_run",
    "examined": 12,
    "eligible": 10,
    "already_readable": 2,
    "seeded": 0,
    "blocked": 0,
}
```

Run:

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_store.py tests\session_bridge/test_cli.py -k "hydration and (backfill or inventory)" -q
```

Expected: FAIL because only exact one-task seeding exists.

### Step 2: Implement read-only inventory

Add `list_sidebar_hydration_candidates(*, now, backfill_days)` in `store.py`.
Return exact source, bridge, and Codex task IDs for visible Claude sidebar jobs
whose source activity falls inside the window and whose lineage is valid. Exclude
rows already present in `session_sidebar_hydration_jobs`.

Use bounded pagination rather than loading the full catalog into memory.

### Step 3: Implement dry-run/apply backend operation

Add `ProductionBackend.sidebar_hydration_seed_backfill` in `cli.py`:

1. Refuse when `legacy_hydration_enabled` is false.
2. Inventory candidates.
3. Read each exact Codex task through `CodexAppServerSidebarDelivery`.
4. Authenticate and classify its first prompt.
5. Build the current last-five-message preview and signed hydration marker only for
   `LEGACY_PLACEHOLDER`.
6. In dry-run, return counts and fixed blocked codes without writing.
7. In apply mode, require the exact confirmation and abort the entire seed phase if
   any candidate is blocked.
8. Seed each eligible job idempotently through `seed_sidebar_hydration_job`.

Add CLI syntax:

```text
hermes session bridge sidebar hydration seed-backfill --days 30
hermes session bridge sidebar hydration seed-backfill --days 30 --apply --confirm HYDRATE_ALL_EXACT_EXISTING_TASKS
```

Do not expose task IDs in the public summary.

### Step 4: Run tests and commit

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_store.py tests\session_bridge/test_cli.py -k "hydration" -q
git add session_bridge/store.py session_bridge/cli.py tests/session_bridge/test_store.py tests/session_bridge/test_cli.py
git commit -m "feat(session-bridge): seed exact legacy hydration backlog"
```

## Task 4: Implement a native in-place hydration executor

**Files:**

- Create: `session_bridge/sidebar_hydration_executor.py`
- Modify: `session_bridge/sidebar_executor.py`
- Test: `tests/session_bridge/test_sidebar_hydration_executor.py`

### Step 1: Write failing executor tests

Cover the complete state machine with a fake native task:

1. no job returns `idle`;
2. pre-existing exact marker commits without sending;
3. unreserved job reads exact task, reserves, starts one turn, verifies the exact
   marker from a fresh client, and commits;
4. reserved retry reconciles only and never calls `turn/start`;
5. timeout after `turn/start` becomes `hydration_send_ambiguous`;
6. an ambiguous retry finds the marker and commits without resend;
7. wrong task ID, wrong cwd/lineage, malformed preview, or wrong marker fail with
   fixed codes;
8. executor never calls `thread/start` or `thread/name/set`.

Run:

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_sidebar_hydration_executor.py -q
```

Expected: FAIL because the native executor does not exist.

### Step 2: Extract reusable native turn primitives

In `sidebar_executor.py`, keep creation behavior unchanged but expose a narrow
`start_text_turn_and_verify_marker` method on
`CodexAppServerSidebarDelivery`. It must:

- call `turn/start` on the supplied exact task ID;
- wait for that exact turn completion;
- open a fresh app-server client;
- `thread/resume` the exact task;
- verify the supplied authenticated marker on the exact turn;
- treat every post-dispatch uncertainty as ambiguous.

### Step 3: Implement `SidebarHydrationExecutor`

The new executor should be synchronous like `SidebarExecutor` and return:

```python
@dataclass(frozen=True)
class SidebarHydrationExecutionResult:
    status: Literal["idle", "visible", "retry", "failed", "unsettled"]
    job_id: str | None = None
    error_code: str | None = None
```

Its locked path:

```python
claim = coordinator.claim_sidebar_hydration_for_delivery(limit=1)
native_prompt = native.read_thread_initial_prompt(thread_id=claim.codex_thread_id)
authenticate_exact_task(native_prompt, claim)
if native.thread_has_marker(claim.codex_thread_id, claim.hydration_marker):
    commit()
elif claim.send_reserved:
    fail("hydration_send_ambiguous")
else:
    reserve()
    native.start_text_turn_and_verify_marker(
        thread_id=claim.codex_thread_id,
        message=claim.hydration_message,
        marker=claim.hydration_marker,
    )
    commit()
```

Use the existing durable lease methods and fixed error allowlists. Since coordinator
claim construction is async, wrap only that call with a private event loop owned by
the recovery thread; never call `asyncio.run` from the service event loop.

### Step 4: Run tests and commit

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_sidebar_hydration_executor.py tests\session_bridge/test_sidebar_executor.py -q
git add session_bridge/sidebar_hydration_executor.py session_bridge/sidebar_executor.py tests/session_bridge/test_sidebar_hydration_executor.py tests/session_bridge/test_sidebar_executor.py
git commit -m "feat(session-bridge): hydrate exact Codex tasks natively"
```

## Task 5: Run one dedicated service-owned recovery worker

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/coordinator.py`
- Test: `tests/session_bridge/test_cli.py`
- Test: `tests/session_bridge/test_coordinator.py`

### Step 1: Write failing worker lifecycle tests

Test that:

- `serve()` starts exactly one recovery thread when sidebar continuous mode is on;
- hydration is attempted before registration;
- when hydration is idle, registration runs immediately;
- successful work loops without the old 60-second/provider-scan delay;
- idle work waits on the stop event and does not busy-loop;
- shutdown sets the event, joins the thread, and closes its isolated backend;
- `_after_successful_scan` registers eligible jobs but does not execute delivery;
- no external automation or task-creation API is called by worker setup.

Run:

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_cli.py tests\session_bridge/test_coordinator.py -k "sidebar and (recovery_worker or post_scan)" -q
```

Expected: FAIL because delivery currently runs only after successful provider scans.

### Step 2: Add `run_sidebar_recovery_once`

Compose a hydration executor and the existing registration executor in one isolated
`ProductionBackend`:

```python
def run_sidebar_recovery_once(self) -> Mapping[str, Any]:
    hydration = self._require_sidebar_hydration_executor().run_once()
    if hydration.status != "idle":
        return {"lane": "hydration", **asdict(hydration)}
    registration = self._require_sidebar_executor().run_once()
    return {"lane": "registration", **asdict(registration)}
```

Both use the same process/store worker lock, which keeps all native task mutations
serialized.

### Step 3: Add and wire the continuous worker

Add `_run_continuous_sidebar_recovery_worker` beside the Claude visibility worker.
Use a short event wait after actionable work and a bounded longer wait when idle or
unsettled. The stop event must interrupt every wait.

In `ProductionBackend.serve`, start this thread after the service composition is
valid and stop/join it in `finally`. Give the thread its own `ProductionBackend`
instance so its DB and app-server lifecycle are isolated from Uvicorn.

Pass `sidebar_executor=None` to `SessionBridgeCoordinator`; keep
`_register_sidebar_after_successful_scan` so scans enqueue newly eligible jobs, but
remove the post-scan delivery call.

### Step 4: Run tests and commit

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_cli.py tests\session_bridge/test_coordinator.py -k "sidebar" -q
git add session_bridge/cli.py session_bridge/coordinator.py tests/session_bridge/test_cli.py tests/session_bridge/test_coordinator.py
git commit -m "fix(session-bridge): decouple sidebar recovery from scans"
```

## Task 6: Extend fixed-code recovery observability

**Files:**

- Modify: `session_bridge/store.py`
- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/mcp_server.py`
- Test: `tests/session_bridge/test_store.py`
- Test: `tests/session_bridge/test_cli.py`
- Test: `tests/session_bridge/test_mcp_server.py`

### Step 1: Write failing status tests

Pin these fields:

- pending, retry, leased, visible, and failed counts for both ledgers;
- oldest pending ages;
- active recovery lane and pending-lane counter;
- recent fixed error codes;
- reserved hydration reconciliation count;
- no source cursors, hashes, signed markers, lease tokens, or raw Codex task IDs.

### Step 2: Implement public status fields

Persist only non-sensitive scheduler progress in `session_bridge_state`.
Expose it through existing status commands and MCP status payloads with strict type
validation and existing redaction helpers.

### Step 3: Run tests and commit

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_store.py tests\session_bridge/test_cli.py tests\session_bridge/test_mcp_server.py -k "sidebar and status" -q
git add session_bridge/store.py session_bridge/cli.py session_bridge/mcp_server.py tests/session_bridge/test_store.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): expose backlog recovery progress"
```

## Task 7: Prove preservation and end-to-end recovery

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Modify: `tests/session_bridge/test_config_safety.py` only if public config shape changes

### Step 1: Add end-to-end regression tests

Create a fixture with:

- four pending Claude sessions of mixed age;
- two exact visible legacy placeholder tasks;
- one already-readable task;
- one hydration send that becomes ambiguous after dispatch.

Assert:

- claim order is newest, newest, newest, oldest;
- every source keeps its original exact task ID;
- each legacy task receives exactly one readable hydration;
- the already-readable task receives none;
- the ambiguous task is reconciled with one send total;
- the worker performs no replacement `thread/start`;
- both ledgers reach zero actionable jobs.

### Step 2: Run focused and full suites

```powershell
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge/test_end_to_end.py -k "sidebar and backlog_recovery" -q
uv run --project C:\Users\diego\.hermes\agent-src --no-sync pytest tests\session_bridge -q
```

Expected: PASS with no warnings or leaked worker threads.

### Step 3: Commit

```powershell
git add tests/session_bridge/test_end_to_end.py tests/session_bridge/test_config_safety.py
git commit -m "test(session-bridge): prove preserve-and-recover backlog drain"
```

## Task 8: Local rollout, guarded hydration, and live canary

**Files:**

- Update only deployment/runtime files already used by the Hermes service.
- Do not edit or recreate a Codex automation.

### Step 1: Capture pre-rollout evidence

Record:

- current service PID/version;
- sidebar and hydration counts;
- active leases;
- newest three eligible Claude source IDs and oldest three legacy source IDs;
- exact linked Codex task IDs for those six sources.

Do not print signed markers, source hashes, or lease tokens.

### Step 2: Integrate locally

Use the repository's local integration path after all tests pass. Confirm no active
sidebar or hydration lease before restarting. Restart the Hermes service once and
verify provider health.

### Step 3: Dry-run the bulk repair

```powershell
hermes session bridge sidebar hydration seed-backfill --days 30
```

Review counts and fixed blocked codes. If any target is unrelated, mismatched, or
unreadable, stop before mutation and investigate that exact lineage.

### Step 4: Apply the approved exact-task repair

```powershell
hermes session bridge sidebar hydration seed-backfill --days 30 --apply --confirm HYDRATE_ALL_EXACT_EXISTING_TASKS
```

This is the only bulk mutation. It seeds durable jobs; it does not send directly.

### Step 5: Verify live canaries

For the captured six sources:

- newest pending sessions appear in Codex promptly;
- old placeholder tasks keep the exact pre-rollout task IDs;
- each legacy task contains one readable summary and last five messages;
- no duplicate `[Claude]` task exists for the same source;
- no `session-sidebar-sync-worker` automation exists.

### Step 6: Monitor to terminal state

Poll the public status until:

```text
sidebar_pending = 0
sidebar_retry = 0
sidebar_leased = 0
hydration_pending = 0
hydration_retry = 0
hydration_leased = 0
blocking_failed_count = 0
```

If a fixed failure code appears, preserve the exact task and stop that job; do not
replace it.

### Step 7: Capture durable project memory

Write one MemPalace record in wing `hermes`, room `session-bridge`, then update the
existing GBrain Session Bridge timeline with the shipped commits, canary evidence,
final queue counts, and the explicit no-replacement result.
