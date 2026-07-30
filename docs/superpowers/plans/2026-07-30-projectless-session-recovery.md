# Projectless Session Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every existing projectless Claude/Hermes import and
automatically recover only untouched bridge-owned tasks into the canonical
`.hermes` Session Inbox through one authenticated native fork.

**Architecture:** Add a recovery-specific durable ledger instead of reopening
completed visibility jobs. Inventory exact visible mirrors, authenticate the
original task and classify every turn, reserve a deterministic fork key, call
`thread/fork` once, bind and verify the returned inbox task, then atomically
rebind the canonical sidebar job/link while preserving immutable
original-to-recovered lineage. Any post-dispatch uncertainty becomes
reconciliation-only.

**Tech Stack:** Python 3.12, SQLite transactional state machines, HMAC/SHA-256
identity, Codex app-server `thread/read`, `thread/fork`, `thread/list`, and
`thread/name/set`, pytest through `scripts/run_tests.sh`.

---

## Dependencies and invariants

- Complete `2026-07-30-session-inbox-placement.md` first.
- Recovery defaults disabled and is enabled only after the placement canary
  passes.
- The original task is never renamed, archived, deleted, or rewritten.
- A task with any substantive Codex work becomes `recovery_manual`.
- `session_sidebar_jobs.codex_thread_id` and the canonical `mirrors` link move
  together in one transaction only after recovered-task verification.
- The immutable recovery ledger retains both original and recovered IDs.
- A reserved fork is never dispatched twice, even after process/laptop crash.
- Zero or multiple recovery-key matches never authorize a new fork.
- Registration and recovery share `_PROCESS_DELIVERY_LOCK`.
- Status exposes fixed reason codes and counts, never source text, source paths,
  markers, task IDs, lease tokens, or raw exceptions.

## File map

- Modify `hermes_state.py`: schema version and recovery tables/indexes.
- Modify `session_bridge/models.py`: recovery state and immutable claim types.
- Create `session_bridge/sidebar_maintenance.py`: bridge-owned turn
  classification and history digests shared with refresh.
- Create `session_bridge/sidebar_recovery_executor.py`: one-lease authenticated
  recovery state machine.
- Modify `session_bridge/sidebar_executor.py`: native read/fork/reconcile
  methods.
- Modify `session_bridge/codex_adapter.py`: exact recovery-key inventory proof.
- Modify `session_bridge/store.py`: inventory, seed, claim, reserve, bind,
  verify, commit, fail, status, and fair scheduler state.
- Modify `session_bridge/config.py` and `hermes_cli/config.py`: strict recovery
  gates and backfill window.
- Modify `session_bridge/cli.py`: operator inventory/apply/status commands and
  continuous recovery lane.
- Modify `session_bridge/mcp_server.py`: sanitized recovery health.
- Modify `tests/test_hermes_state.py`.
- Modify `tests/hermes_state/test_session_bridge_schema.py`.
- Create `tests/session_bridge/test_sidebar_maintenance.py`.
- Create `tests/session_bridge/test_sidebar_recovery_executor.py`.
- Modify `tests/session_bridge/test_sidebar_executor.py`.
- Modify `tests/session_bridge/test_target_adapters.py`.
- Modify `tests/session_bridge/test_store.py`.
- Modify `tests/session_bridge/test_cli.py`.
- Modify `tests/session_bridge/test_mcp_server.py`.
- Modify `tests/session_bridge/test_end_to_end.py`.
- Modify `tests/session_bridge/test_config_safety.py`.

## Task 1: Define recovery identity, ownership, and schema

**Files:**

- Modify: `session_bridge/models.py`
- Create: `session_bridge/sidebar_maintenance.py`
- Create: `tests/session_bridge/test_sidebar_maintenance.py`
- Modify: `hermes_state.py`
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/hermes_state/test_session_bridge_schema.py`

- [ ] **Step 1: Write failing bridge-owned-history tests**

Build projections containing:

- the authentic readable registration plus `REGISTERED`;
- an authentic hydration turn plus `HYDRATED`;
- an authentic refresh turn plus `REFRESHED`;
- an interrupted bridge maintenance acknowledgement;
- a normal user question;
- a normal assistant answer;
- a tool/command/file-change item.

Require:

```python
assert classify_sidebar_history(bridge_only, secret).automatic_safe is True
assert classify_sidebar_history(user_question, secret).reason_code == (
    "recovery_substantive_work"
)
assert classify_sidebar_history(tool_work, secret).reason_code == (
    "recovery_substantive_work"
)
assert classify_sidebar_history(forged_marker, secret).reason_code == (
    "recovery_identity_mismatch"
)
```

The classifier must allow only authenticated bridge user prompts and the fixed
assistant acknowledgements `REGISTERED`, `HYDRATED`, and `REFRESHED`.
Whitespace variants, extra prose, tool calls, commands, file changes, approval
requests, user-input requests, and unknown roles are substantive/unsafe.

- [ ] **Step 2: Define exact states and claims**

Add:

```python
class SidebarRecoveryState(StrEnum):
    PENDING = "recovery_pending"
    LEASED = "recovery_leased"
    RESERVED = "recovery_reserved"
    BOUND = "recovery_bound"
    VERIFIED = "recovery_verified"
    RETRY = "recovery_retry"
    AMBIGUOUS = "recovery_ambiguous"
    COMMITTED = "recovery_committed"
    MANUAL = "recovery_manual"


@dataclass(frozen=True)
class SidebarHistoryClassification:
    automatic_safe: bool
    history_digest: str
    turn_count: int
    reason_code: str | None
```

The history digest is SHA-256 over canonical JSON containing exact ordered
role/content/tool identity, not timestamps or presentation formatting.

- [ ] **Step 3: Write failing schema tests**

Require `session_sidebar_recovery_jobs` with:

```text
id, source_session_id, bridge_id, original_thread_id,
original_target_session_id, original_history_digest, recovery_key,
placement_generation, state, attempts, next_attempt_at,
lease_digest, lease_expires_at, fork_reserved_at,
recovered_thread_id, recovered_target_session_id,
bound_at, verified_at, committed_at, completion_digest,
error_code, created_at, updated_at
```

Require `session_sidebar_recovery_lineage` with:

```text
recovery_job_id, source_session_id, bridge_id,
original_thread_id, recovered_thread_id,
original_target_session_id, recovered_target_session_id,
original_history_digest, placement_generation, committed_at
```

The lineage table is append-only through `BEFORE UPDATE` and `BEFORE DELETE`
triggers. Add due, lease, recovery-key, original-ID, recovered-ID, and state
indexes. Bump `SCHEMA_VERSION` exactly once and prove reopening old/current
databases preserves existing rows.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_maintenance.py tests/hermes_state/test_session_bridge_schema.py tests/test_hermes_state.py -k "sidebar and recovery" -q'
```

Expected: FAIL because the states, classifier, and tables do not exist.

- [ ] **Step 4: Implement and commit**

```powershell
git add hermes_state.py session_bridge/models.py session_bridge/sidebar_maintenance.py tests/test_hermes_state.py tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_sidebar_maintenance.py
git commit -m "feat(session-bridge): define projectless recovery ledger"
```

## Task 2: Implement durable recovery inventory and transitions

**Files:**

- Modify: `session_bridge/store.py`
- Modify: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing inventory and seed tests**

Seed exact visible sidebar rows for:

1. canonical inbox cwd;
2. projectless/outside-inbox cwd with `mirrors`;
3. outside-inbox cwd with `continues`;
4. outside-inbox task already in recovery;
5. older than the configured backfill;
6. missing/corrupt target lineage.

Require `list_sidebar_recovery_candidates` to return only case 2, ordered
newest first, with original thread ID, source/bridge IDs, source cwd, exact
title, target session ID, and source cursor/hash. The query must not classify
native history; it only produces bounded candidates for exact native reads.

Require `seed_sidebar_recovery_job` to be idempotent for one exact
identity and reject any changed original task, target session, bridge,
placement generation, history digest, or recovery key.

- [ ] **Step 2: Write failing transition and crash tests**

Cover:

```text
pending -> leased -> reserved -> bound -> verified -> committed
leased pre-dispatch failure -> retry
reserved expiry -> ambiguous
bound expiry -> retry without clearing fork_reserved_at or recovered_thread_id
identity/substantive conflict -> manual
```

Assert:

- one active lease globally;
- lease/completion digests never persist raw tokens;
- reserve is a compare-and-swap and replay returns the same recovery key;
- an ambiguous row can be leased for reconciliation but retains
  `fork_reserved_at`;
- a bound row can resume verification but can never dispatch another fork;
- max-attempt failure becomes manual with a fixed code;
- unknown codes are rejected.

- [ ] **Step 3: Implement fixed recovery errors**

Add:

```python
RECOVERY_RETRYABLE_ERRORS = frozenset({
    "recovery_original_unreadable",
    "recovery_native_rejected",
    "native_task_not_indexed",
    "bridge_temporarily_unavailable",
    "broker_time_budget",
})

RECOVERY_AMBIGUOUS_ERRORS = frozenset({
    "recovery_ambiguous",
})

RECOVERY_MANUAL_ERRORS = frozenset({
    "recovery_identity_mismatch",
    "recovery_substantive_work",
    "placement_mismatch",
})
```

Persist only these fixed codes.

- [ ] **Step 4: Implement the atomic canonical rebind**

`commit_sidebar_recovery_job` must in one `BEGIN IMMEDIATE` transaction:

1. authenticate the leased/verified recovery row;
2. prove the original sidebar job is still visible and points to
   `original_thread_id`;
3. prove the only canonical link is still `mirrors` to
   `original_target_session_id`;
4. prove the recovered indexed target has the same bridge provenance;
5. update `session_sidebar_jobs.codex_thread_id` to `recovered_thread_id`;
6. update that exact `session_links.to_session_id` to the recovered target;
7. insert immutable recovery lineage;
8. mark recovery committed with a completion digest.

Any failed assertion rolls back every change. Exact completion replay returns
the same committed row and lineage.

- [ ] **Step 5: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_store.py -k "sidebar_recovery or recovery_lineage or canonical_rebind" -q'
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): persist exact task recovery"
```

## Task 3: Add exact native fork and reconciliation primitives

**Files:**

- Modify: `session_bridge/sidebar_executor.py`
- Modify: `session_bridge/codex_adapter.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`
- Modify: `tests/session_bridge/test_target_adapters.py`

- [ ] **Step 1: Write failing `thread/fork` request tests**

Require exactly:

```python
{
    "threadId": ORIGINAL_THREAD_ID,
    "cwd": placement.inbox_cwd,
    "runtimeWorkspaceRoots": list(placement.runtime_workspace_roots),
    "threadSource": recovery_key,
}
```

The returned thread must have one exact nonempty ID, matching recovery key, and
inbox cwd. A pre-dispatch budget/inbox failure is definite. Once `request`
starts, timeout, disconnect, malformed response, missing ID, wrong cwd, or
wrong recovery key raises `NativeForkAmbiguous`.

- [ ] **Step 2: Add narrow protocol methods**

Extend `NativeSidebarDelivery`:

```python
def read_thread_projection(
    self, *, thread_id: str, deadline: float
) -> SessionProjection:
    raise NotImplementedError

def fork_thread(
    self,
    *,
    original_thread_id: str,
    placement: SidebarPlacement,
    recovery_key: str,
    deadline: float,
) -> str:
    raise NotImplementedError
```

Reuse `find_by_recovery_key` with `expected_cwd=placement.inbox_cwd` for fork
reconciliation. Require exactly zero or one result; conflicting cwd or
duplicate IDs is `codex_thread_conflict`.

- [ ] **Step 3: Prove copied history and original immutability**

Read the original immediately before reserve and compute the canonical history
digest. After fork:

- read the recovered task through a fresh normal client;
- require inbox cwd and exact marker/source identity;
- require the recovered bridge-owned history digest equals the stored original
  digest;
- re-read the original and require its digest is unchanged;
- require both tasks are quiescent.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py -k "fork or recovery_key or original_history" -q'
```

Expected: PASS after implementation.

- [ ] **Step 4: Commit**

```powershell
git add session_bridge/sidebar_executor.py session_bridge/codex_adapter.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py
git commit -m "feat(session-bridge): add authenticated native task fork"
```

## Task 4: Build the one-lease recovery executor

**Files:**

- Create: `session_bridge/sidebar_recovery_executor.py`
- Create: `tests/session_bridge/test_sidebar_recovery_executor.py`

- [ ] **Step 1: Write failing executor tests**

Use fake store/native boundaries to prove:

- no claim returns `idle` with no native call;
- unsafe original becomes manual before reserve/fork;
- safe pending row reserves, forks once, binds, verifies, renames, indexes, and
  commits;
- reserved ambiguity reconciles by key and never calls `fork_thread`;
- zero reconciliation result remains ambiguous and never calls `fork_thread`;
- one exact result binds and resumes verification;
- multiple/conflicting results become manual;
- a store failure after returned ID retains that ID and never forks again;
- relation changed to `continues` before commit becomes manual;
- every call is under `_PROCESS_DELIVERY_LOCK`.

- [ ] **Step 2: Implement the executor result and claim validation**

Use:

```python
@dataclass(frozen=True)
class SidebarRecoveryExecutionResult:
    status: Literal["idle", "committed", "retry", "manual", "ambiguous", "unsettled"]
    error_code: str | None = None
```

The executor sequence is:

```text
claim
-> resolve placement
-> read/authenticate/classify original
-> verify stored history digest
-> reconcile if fork_reserved_at exists
-> otherwise reserve then fork once
-> bind returned/reconciled ID
-> fresh-client verify recovered and original
-> rename recovered
-> index exact recovered projection
-> atomic commit/rebind
```

Do not catch `KeyboardInterrupt` or `SystemExit`. Convert all other failures to
fixed codes through one settlement call.

- [ ] **Step 3: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_recovery_executor.py -q'
git add session_bridge/sidebar_recovery_executor.py tests/session_bridge/test_sidebar_recovery_executor.py
git commit -m "feat(session-bridge): execute preserve-and-recover forks"
```

## Task 5: Add operator inventory, rollout gates, and fair continuous scheduling

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/config.py`
- Modify: `hermes_cli/config.py`
- Modify: `session_bridge/store.py`
- Modify: `session_bridge/mcp_server.py`
- Modify: `tests/session_bridge/test_config_safety.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_store.py`
- Modify: `tests/session_bridge/test_mcp_server.py`

- [ ] **Step 1: Write failing CLI safety tests**

Add:

```text
sidebar-recovery-inventory --days 30 --limit 5
sidebar-recovery-seed --source-session-id claude:canary-source
  --original-thread-id 019fb0b2-1733-7891-8d19-c84a592f3254
  --confirm PRESERVE_AND_FORK_EXACT_BRIDGE_TASK
sidebar-recovery-status --json
sidebar-recovery --enable|--disable
```

Inventory is read-only. Seed rejects missing confirmation, mismatched exact
identity, a task with substantive work, and an inbox-rooted task. `--enable`
requires the placement canary state to be successful.

- [ ] **Step 2: Add strict recovery configuration**

Extend `SidebarConfig`:

```python
recovery_enabled: bool = False
recovery_backfill_days: int = 30
```

Accept only these TOML keys, add no environment variables, and require
`recovery_backfill_days` between `1` and `365`. The installed default remains
disabled. The continuous worker may inventory while disabled but must not lease,
reserve, fork, rename, or rebind.

- [ ] **Step 3: Compose the executor**

Add `_require_sidebar_recovery_executor()` using:

- the normal native delivery for original reads/forks/renames;
- a fresh normal-client factory for persistence proof;
- the existing marker key;
- the placement resolver from the first plan;
- direct store claim/transition methods.

Close/recycle all clients without double-close.

- [ ] **Step 4: Implement durable fair lane choice**

Persist a versioned scheduler record:

```python
{
    "version": 1,
    "ordinary_next": "recovery",
    "last_lane": "registration",
    "at": now,
}
```

Each continuous cycle:

1. reconcile any ambiguous recovery first;
2. attempt one due new registration;
3. if registration is idle, attempt one recovery;
4. after a recovery action, return to registration on the next cycle;
5. after at most three consecutive registration actions while recovery is due,
   run one recovery.

The store updates scheduler state atomically with the selected lane. Expired
leases are repaired before selection. Hydration remains a compatibility lane
but cannot starve registration/recovery.

Extend the persisted recovery-progress contract to accept lane `recovery` and
statuses `committed`, `manual`, and `ambiguous` in addition to the existing
registration/hydration statuses. The public status shaper uses the same fixed
allowlist and rejects all other values.

- [ ] **Step 5: Shape sanitized status**

Expose:

```python
{
    "enabled": True,
    "counts": {state.value: count for state in SidebarRecoveryState},
    "awaiting_recovery": 17,
    "recovered": 5,
    "manual": 2,
    "ambiguous": 0,
    "recent_error_codes": ["recovery_substantive_work"],
    "latency_seconds": {"p50": 12.0, "p95": 30.0},
}
```

No task/source IDs or non-inbox paths.

- [ ] **Step 6: Run and commit**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_config_safety.py tests/session_bridge/test_cli.py tests/session_bridge/test_store.py tests/session_bridge/test_mcp_server.py -k "sidebar and recovery" -q'
git add session_bridge/config.py hermes_cli/config.py session_bridge/cli.py session_bridge/store.py session_bridge/mcp_server.py tests/session_bridge/test_config_safety.py tests/session_bridge/test_cli.py tests/session_bridge/test_store.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): schedule projectless task recovery"
```

## Task 6: End-to-end crash proof and staged live recovery

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Verify only: production runtime

- [ ] **Step 1: Add restart-boundary integration cases**

For each crash point, restart with the same SQLite database:

1. after recovery lease;
2. after fork reservation before dispatch;
3. after fork dispatch before response;
4. after returned ID before bind;
5. after bind before verification;
6. after verification before atomic commit.

Require at most one `thread/fork`, one recovered task ID, one lineage row, one
canonical mirror link, unchanged original history, and eventual drain.

- [ ] **Step 2: Run focused and complete suites**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_maintenance.py tests/session_bridge/test_sidebar_recovery_executor.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py tests/session_bridge/test_store.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_end_to_end.py -q'
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/ -q --file-timeout 900'
```

- [ ] **Step 3: Execute the approved canary ladder**

1. keep recovery disabled and inventory 30 days;
2. create one disposable projectless bridge canary;
3. seed/recover only that exact canary;
4. prove original unchanged/unarchived, recovered under `.hermes`, and one
   canonical link;
5. enable and recover the five newest eligible untouched real imports;
6. inspect all five in the Codex `.hermes` project and verify title,
   Continuation Brief, last five messages, project ID, and canonical link;
7. stop if any task is substantive, ambiguous, mismatched, or projectless;
8. only then seed the remaining eligible 30-day inventory one at a time.

- [ ] **Step 4: Commit**

```powershell
git add tests/session_bridge/test_end_to_end.py
git commit -m "test(session-bridge): prove crash-safe placement recovery"
```

## Completion gate

- [ ] Every automatic candidate is bridge-owned and quiescent.
- [ ] Original tasks remain unchanged and unarchived.
- [ ] Fork reservation survives crashes and prevents duplicate forks.
- [ ] Canonical sidebar job/link rebind is atomic.
- [ ] Immutable lineage records both exact task IDs.
- [ ] Substantive tasks are manual, not mutated.
- [ ] New registration and recovery do not starve each other.
- [ ] Focused and complete suites pass.
- [ ] One disposable and five real canaries pass with zero ambiguity.
