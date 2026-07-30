# Session Readable Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the Continuation Brief, last five messages, and Codex recency of
active untouched Claude/Hermes mirrors current through bounded, signed,
coalesced refresh turns.

**Architecture:** Detect source cursor/hash advances during the existing
sidebar catalog pass, coalesce each source to its newest snapshot, and schedule
at most one active refresh per 15 minutes plus one final quiet refresh after 60
seconds. A separate durable send ledger reserves immediately before the exact
native turn; after dispatch uncertainty, only exact marker reconciliation may
commit. Refresh eligibility is rechecked against canonical inbox placement,
bridge-owned task history, quiescence, and a still-`mirrors` relation.

**Tech Stack:** Python 3.12, deterministic preview rendering, HMAC/SHA-256
markers, SQLite latest-wins queues, Codex app-server text turns, pytest through
`scripts/run_tests.sh`.

---

## Dependencies and invariants

- Complete `2026-07-30-session-inbox-placement.md` and
  `2026-07-30-projectless-session-recovery.md` first.
- Refresh defaults disabled.
- One source session has at most one open refresh row.
- Coalescing never overwrites a reserved/ambiguous snapshot.
- The exact refresh marker is authenticated and appears once in the sent
  message.
- After native send dispatch, uncertainty never authorizes another send.
- Refresh stops permanently after the canonical relation becomes `continues`.
- Any substantive Codex work makes the task ineligible and manual; it is never
  refreshed.
- Refresh uses the existing preview builder and character budget; it is not a
  full transcript migration.
- Registration, recovery, hydration, and refresh share
  `_PROCESS_DELIVERY_LOCK`.
- Public status contains digests/counts/timing only, not messages, source paths,
  markers, tokens, or exact task IDs.

## File map

- Modify `hermes_state.py`: refresh schema and indexes.
- Modify `session_bridge/models.py`: refresh state and marker payload.
- Modify `session_bridge/sidebar.py`: refresh marker codec and message builder.
- Modify `session_bridge/sidebar_maintenance.py`: recognize authentic refresh
  turns as bridge-owned.
- Create `session_bridge/sidebar_refresh_executor.py`: one-lease refresh
  reconciler/sender.
- Modify `session_bridge/sidebar_executor.py`: reuse exact marker read/send
  primitives and expose quiescent projection.
- Modify `session_bridge/store.py`: latest-wins queue, throttle accounting,
  claim/reserve/commit/fail/status, continuation cancellation.
- Modify `session_bridge/coordinator.py`: enqueue refresh candidates from the
  existing Claude/Hermes sidebar catalog pass.
- Modify `session_bridge/cli.py`: configuration, executor composition,
  fair-lane scheduling, operator status/gates.
- Modify `session_bridge/mcp_server.py`: sanitized refresh health.
- Modify `session_bridge/config.py` and `hermes_cli/config.py`: strict settings.
- Modify `tests/test_hermes_state.py`.
- Modify `tests/hermes_state/test_session_bridge_schema.py`.
- Modify `tests/session_bridge/test_sidebar.py`.
- Modify `tests/session_bridge/test_sidebar_maintenance.py`.
- Create `tests/session_bridge/test_sidebar_refresh_executor.py`.
- Modify `tests/session_bridge/test_store.py`.
- Modify `tests/session_bridge/test_coordinator.py`.
- Modify `tests/session_bridge/test_sidebar_executor.py`.
- Modify `tests/session_bridge/test_cli.py`.
- Modify `tests/session_bridge/test_mcp_server.py`.
- Modify `tests/session_bridge/test_end_to_end.py`.

## Task 1: Define the signed refresh payload and exact maintenance message

**Files:**

- Modify: `session_bridge/models.py`
- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/sidebar_maintenance.py`
- Modify: `tests/session_bridge/test_sidebar.py`
- Modify: `tests/session_bridge/test_sidebar_maintenance.py`

- [ ] **Step 1: Write failing marker round-trip tests**

Add:

```python
@dataclass(frozen=True)
class RefreshMarkerPayload:
    bridge_id: str
    codex_thread_id: str
    preview_digest: str
    refresh_generation: int
    source_cursor: str
    source_hash: str
    source_session_id: str
```

Require canonical `HERMES_SESSION_REFRESH_V1:<body>.<signature>` encoding,
round-trip decoding, deterministic field ordering, exact task/source/bridge
identity, lowercase SHA-256 preview digest, and generation exactly `1`.
Reject altered signatures, padding, duplicate/unknown/missing fields, newline
in any identity, booleans, noncanonical JSON, and embedded marker boundaries.

- [ ] **Step 2: Write failing message-builder tests**

Require:

```python
message = build_refresh_message(
    preview_rendered=preview.rendered,
    source_session_id=SOURCE_ID,
    refresh_marker=marker,
)
assert message.endswith(
    f"Refresh marker: {marker}\n"
    "Do not call session_continue or perform project work during this turn.\n"
    "Reply only: REFRESHED"
)
assert message.count(marker) == 1
```

The message must start with the existing bounded `# Imported` preview and
contain no extra transcript outside that preview.

- [ ] **Step 3: Extend bridge-owned classification**

An authentic refresh user turn followed by exact `REFRESHED` is bridge-owned.
A forged marker, wrong task ID, wrong source, extra assistant prose, tool use,
command, or user reply remains substantive/unsafe.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar.py tests/session_bridge/test_sidebar_maintenance.py -k "refresh" -q'
```

Expected: FAIL because the payload and builder do not exist.

- [ ] **Step 4: Implement and commit**

```powershell
git add session_bridge/models.py session_bridge/sidebar.py session_bridge/sidebar_maintenance.py tests/session_bridge/test_sidebar.py tests/session_bridge/test_sidebar_maintenance.py
git commit -m "feat(session-bridge): authenticate readable refresh turns"
```

## Task 2: Add strict refresh configuration and durable schema

**Files:**

- Modify: `session_bridge/config.py`
- Modify: `hermes_cli/config.py`
- Modify: `tests/session_bridge/test_config_safety.py`
- Modify: `hermes_state.py`
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/hermes_state/test_session_bridge_schema.py`

- [ ] **Step 1: Write failing config tests**

Extend `SidebarConfig`:

```python
refresh_enabled: bool = False
refresh_active_interval_seconds: int = 900
refresh_quiet_seconds: int = 60
refresh_max_per_hour: int = 20
```

Only these TOML keys are accepted. Require active interval `>= 60`, quiet
seconds between `10` and active interval, and hourly maximum between `1` and
`100`. Reject booleans as integers and add no environment variables.

- [ ] **Step 2: Write failing schema tests**

Require `session_sidebar_refresh_jobs`:

```text
id, source_session_id, bridge_id, codex_thread_id,
source_cursor, source_hash, preview_version, preview_digest,
refresh_marker, refresh_generation, source_advanced_at, quiet_due_at,
delivery_kind, state, attempts, next_attempt_at,
lease_digest, lease_expires_at, send_reserved_at,
sent_at, verified_at, committed_at, completion_digest,
error_code, created_at, updated_at
```

States:

```python
class SidebarRefreshState(StrEnum):
    PENDING = "refresh_pending"
    LEASED = "refresh_leased"
    RETRY = "refresh_retry"
    AMBIGUOUS = "refresh_ambiguous"
    COMMITTED = "refresh_committed"
    MANUAL = "refresh_manual"
    CANCELLED = "refresh_cancelled"
```

`delivery_kind` is exactly `active` or `quiet`. Add due, lease, task, source,
marker, state, and committed-time indexes. Existing databases upgrade without
changing registration, hydration, or recovery rows.

- [ ] **Step 3: Implement and run**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_config_safety.py tests/hermes_state/test_session_bridge_schema.py tests/test_hermes_state.py -k "sidebar and refresh" -q'
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add session_bridge/config.py hermes_cli/config.py hermes_state.py tests/session_bridge/test_config_safety.py tests/hermes_state/test_session_bridge_schema.py tests/test_hermes_state.py
git commit -m "feat(session-bridge): persist readable refresh settings"
```

## Task 3: Implement latest-wins queueing, throttles, and continuation stop

**Files:**

- Modify: `session_bridge/store.py`
- Modify: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing eligibility/coalescing tests**

`coalesce_sidebar_refresh` must require:

- visible canonical sidebar job;
- exact current `mirrors` link;
- no open recovery or recovery ambiguity;
- source cursor/hash differs from last committed registration/hydration/refresh
  snapshot;
- exact canonical Codex thread ID;
- canonical inbox placement generation;
- refresh enabled by caller.

For an unreserved pending/retry row, a newer source replaces cursor, hash,
preview digest, marker, source-advanced time, and quiet due time in place. For
a leased/reserved/ambiguous row, preserve it byte-for-byte and create/update one
successor row keyed by the newer snapshot. Thus at most one immutable in-flight
row and one coalesced successor exist.

- [ ] **Step 2: Write failing schedule tests**

At `now`:

- active delivery is due only when the last committed refresh is at least 900
  seconds old and source activity is continuing;
- quiet delivery is due at `last_source_advance + 60`;
- quiet wins over active for the same snapshot;
- reserved/ambiguous reconciliation wins over both;
- no more than 20 committed sends in `(now - 3600, now]`;
- global throttle postpones `next_attempt_at` to the oldest counted commit plus
  3600 seconds;
- cursor/hash coalescing delivers only the newest source snapshot.

- [ ] **Step 3: Write failing continuation cancellation test**

Extend `transition_link_to_continues` so the same transaction changes all
unreserved pending/retry refresh rows for the bridge to `refresh_cancelled`
with fixed `error_code="continued"`. If any row has `send_reserved_at`, abort
the link transition with `refresh_send_ambiguous` until exact reconciliation
settles it.

Once continued, later `coalesce_sidebar_refresh` calls return
`{"created": False, "reason": "continued"}` and create no row.

- [ ] **Step 4: Implement lease/reserve/commit/fail**

Required methods:

```text
coalesce_sidebar_refresh
claim_sidebar_refresh_jobs
reserve_sidebar_refresh_send
commit_sidebar_refresh_job
fail_sidebar_refresh_job
sidebar_refresh_status
```

Use only these fixed failure classes:

```python
REFRESH_RETRYABLE_ERRORS = frozenset({
    "refresh_target_unreadable",
    "native_task_not_indexed",
    "bridge_temporarily_unavailable",
    "broker_time_budget",
})

REFRESH_AMBIGUOUS_ERRORS = frozenset({
    "refresh_send_ambiguous",
})

REFRESH_MANUAL_ERRORS = frozenset({
    "refresh_identity_mismatch",
    "placement_mismatch",
    "refresh_substantive_work",
})
```

Rules:

- one refresh lease globally;
- raw tokens are never stored;
- reserve is immediate before native send;
- expired unreserved lease becomes retry;
- expired reserved lease becomes ambiguous;
- ambiguous claim is reconciliation-only;
- exact marker completion replay is idempotent;
- `refresh_send_ambiguous` preserves reservation and snapshot forever;
- commit records delivered cursor/hash/digest and timestamp.

- [ ] **Step 5: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_store.py -k "sidebar_refresh or refresh_coalesce or refresh_throttle or continued" -q'
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): coalesce durable preview refreshes"
```

## Task 4: Enqueue source advances from the existing catalog pass

**Files:**

- Modify: `session_bridge/coordinator.py`
- Modify: `tests/session_bridge/test_coordinator.py`

- [ ] **Step 1: Write failing Claude and Hermes enqueue tests**

In `_register_sidebar_jobs_locked`, when a source already has a visible sidebar
job:

- Claude cursor/hash advance coalesces one refresh;
- profile Hermes cursor/hash advance coalesces one refresh;
- unchanged source creates none;
- source advances during open recovery create a deferred successor;
- continued source creates none;
- malformed one-source metadata increments one fixed failure and does not stop
  other candidates.

The existing newest-probe, catch-up cursor, registration limits, and source
eligibility behavior remain unchanged.

- [ ] **Step 2: Add a narrow store call before the existing `continue`**

Use:

```python
if existing is not None:
    if (
        apply
        and self._config.sidebar.refresh_enabled
        and existing.get("state") == SidebarJobState.VISIBLE.value
    ):
        await asyncio.to_thread(
            _call,
            self._store,
            "coalesce_sidebar_refresh",
            source_session_id=canonical_source,
            bridge_id=sidebar_bridge_id(canonical_source),
            codex_thread_id=existing["codex_thread_id"],
            source_cursor=projection.native_cursor,
            source_hash=projection.native_hash,
            source_advanced_at=projection.last_active,
            quiet_due_at=projection.last_active
            + self._config.sidebar.refresh_quiet_seconds,
            now=registration_time,
        )
    continue
```

The store, not the coordinator, builds or persists the signed snapshot
identity. If a source lacks cursor/hash, record a fixed candidate failure and
do not synthesize identity.

- [ ] **Step 3: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_coordinator.py -k "sidebar and refresh" -q'
git add session_bridge/coordinator.py tests/session_bridge/test_coordinator.py
git commit -m "feat(session-bridge): enqueue active source refreshes"
```

## Task 5: Build the exact refresh executor

**Files:**

- Create: `session_bridge/sidebar_refresh_executor.py`
- Create: `tests/session_bridge/test_sidebar_refresh_executor.py`
- Modify: `session_bridge/sidebar_executor.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`

- [ ] **Step 1: Write failing executor tests**

Prove:

- no claim is idle/no native call;
- target unreadable before reserve retries safely;
- wrong task/source/marker/cwd/history becomes manual;
- substantive work becomes manual;
- continued relation before reserve cancels;
- existing exact refresh marker commits without resend;
- reserved row with missing marker remains ambiguous and never sends;
- fresh row builds the exact latest preview, reserves, sends once, verifies the
  exact marker in a completed turn, and commits;
- post-dispatch timeout becomes `refresh_send_ambiguous`;
- crash after reserve never calls send again;
- all native mutation runs under `_PROCESS_DELIVERY_LOCK`.

- [ ] **Step 2: Define the result and claim**

```python
@dataclass(frozen=True)
class SidebarRefreshExecutionResult:
    status: Literal[
        "idle", "committed", "retry", "manual", "ambiguous", "unsettled"
    ]
    error_code: str | None = None
```

The claim includes exact source/task/bridge IDs, source cursor/hash, preview
digest/version, marker, delivery kind, source cwd, and `send_reserved`.

- [ ] **Step 3: Implement the executor**

Sequence:

```text
claim
-> read exact task and verify inbox placement/quiescence
-> authenticate registration/source identity
-> classify all history as bridge-owned
-> recheck relation is mirrors and snapshot is still current
-> if exact marker exists, commit
-> if send_reserved and marker absent, settle ambiguous
-> rebuild preview and require exact stored digest
-> reserve
-> start one text turn with exact message/marker
-> fresh-client verify exact completed marker
-> commit delivered snapshot
```

Reuse `start_text_turn_and_verify_marker`; expand its authenticated marker
regular expression to include `HERMES_SESSION_REFRESH_V1`. Do not add a second
send implementation.

- [ ] **Step 4: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_refresh_executor.py tests/session_bridge/test_sidebar_executor.py -k "refresh" -q'
git add session_bridge/sidebar_refresh_executor.py session_bridge/sidebar_executor.py tests/session_bridge/test_sidebar_refresh_executor.py tests/session_bridge/test_sidebar_executor.py
git commit -m "feat(session-bridge): deliver signed readable refreshes"
```

## Task 6: Integrate fair scheduling and sanitized status

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/store.py`
- Modify: `session_bridge/mcp_server.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_store.py`
- Modify: `tests/session_bridge/test_mcp_server.py`

- [ ] **Step 1: Write failing lane-order tests**

Require each continuous cycle to choose in this order:

1. ambiguous recovery reconciliation;
2. ambiguous refresh reconciliation;
3. new registration latency slot;
4. ordinary lane alternating recovery and quiet refresh;
5. throttled active refresh;
6. legacy hydration only when it has already-reserved reconciliation, otherwise
   as an ordinary compatibility lane.

After three consecutive registration actions while recovery/quiet refresh is
due, one ordinary lane must run. A nonempty recovery backlog cannot suppress
new registration, and continuous registration cannot permanently suppress
recovery/quiet refresh. Persist the next ordinary lane so restart does not reset
fairness.

Extend the persisted recovery-progress contract with lane `refresh` and
statuses `committed`, `manual`, and `ambiguous`. Keep `idle`, `retry`, and
`unsettled`; reject every other lane/status pair in both store and public
shapers.

- [ ] **Step 2: Compose the refresh executor**

Add `_require_sidebar_refresh_executor()` using the normal native client,
marker key, store, placement resolver, preview budget, and strict config
intervals. Add it to close/recycle lifecycle without affecting the lean
registration client.

- [ ] **Step 3: Shape status and health**

Expose:

```python
{
    "enabled": True,
    "counts": {state.value: count for state in SidebarRefreshState},
    "reserved_reconciliation": 0,
    "committed": 42,
    "recent_error_codes": [],
    "last_delivered": {
        "source_identity_digest": "lowercase-sha256",
        "cursor_hash_digest": "lowercase-sha256",
        "at": 1234.0,
    },
    "latency_seconds": {"p50": 15.0, "p95": 60.0},
    "hourly": {"used": 3, "limit": 20},
}
```

Use only digests; never render raw source cursor/hash. Reject unknown states,
unknown codes, NaN/infinity, negative counts, and hostile mappings.

- [ ] **Step 4: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_store.py tests/session_bridge/test_mcp_server.py -k "sidebar and (refresh or recovery_progress or scheduler)" -q'
git add session_bridge/cli.py session_bridge/store.py session_bridge/mcp_server.py tests/session_bridge/test_cli.py tests/session_bridge/test_store.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): schedule and report readable refresh"
```

## Task 7: End-to-end refresh, crash, and rollout proof

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Verify only: production runtime

- [ ] **Step 1: Add end-to-end timing and crash cases**

Use a controllable clock and one existing inbox-rooted untouched mirror:

- three source advances inside 15 minutes produce one newest-snapshot active
  refresh;
- another source advance resets quiet due time;
- 60 seconds of silence produces one final quiet refresh;
- relation transition to `continues` prevents all later refreshes;
- crash after reservation, send dispatch, marker persistence, and before commit
  each reconciles without a duplicate turn;
- restart drains registration, recovery, quiet refresh, and active refresh
  fairly.

- [ ] **Step 2: Run focused and complete suites**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar.py tests/session_bridge/test_sidebar_maintenance.py tests/session_bridge/test_sidebar_refresh_executor.py tests/session_bridge/test_sidebar_recovery_executor.py tests/session_bridge/test_store.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_end_to_end.py -q'
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/ -q --file-timeout 900'
```

- [ ] **Step 3: Run the live refresh canary**

1. keep active refresh disabled;
2. enable quiet refresh only;
3. use one disposable active Claude source with an already verified inbox task;
4. append multiple meaningful source turns within 60 seconds;
5. verify exactly one quiet refresh arrives after settling, contains the newest
   Continuation Brief and last five messages, and advances task recency;
6. enable active refresh and sustain source activity past 15 minutes;
7. verify at most one active refresh per interval and no more than 20/hour;
8. send one substantive Codex message that performs `session_continue`;
9. verify relation becomes `continues` and no later source activity generates a
   refresh;
10. restart the bridge during one disposable reserved refresh and prove exact
    marker reconciliation with no duplicate send.

- [ ] **Step 4: Commit**

```powershell
git add tests/session_bridge/test_end_to_end.py
git commit -m "test(session-bridge): prove coalesced session refresh"
```

## Completion gate

- [ ] Refresh marker and preview digest are authenticated.
- [ ] Pending updates coalesce to the newest cursor/hash.
- [ ] Sustained activity respects 15-minute per-source throttling.
- [ ] Quiet activity produces one final refresh after 60 seconds.
- [ ] Global delivery never exceeds 20 committed refreshes/hour.
- [ ] Reserved ambiguity never resends.
- [ ] Substantive/continued tasks never refresh.
- [ ] Registration, recovery, and refresh remain fair after restart.
- [ ] Focused and complete suites pass.
- [ ] Live quiet, active, continuation-stop, and crash canaries pass.
