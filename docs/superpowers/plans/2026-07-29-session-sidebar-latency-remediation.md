# Session Sidebar Latency Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkboxes so progress survives interruption. This repository forbids
> subagents for this work, so execute inline and in order.

**Goal:** Make newly meaningful Claude Code and Hermes sessions appear in the
Codex sidebar within 30 seconds at p95 when no backlog exists, and drain ten
eligible sessions within three minutes, without weakening exact-task or
no-blind-retry safety.

**Architecture:** Keep the durable scanner, newest-session probe, single-writer
claim, reservation, and persistence-verification flow. Split the sidebar
provider runtime into a lean registration-only Codex app-server process and a
normal read/hydration process. The lean process uses the real Codex home and
session store but disables configured MCP servers and plugins only for that
process. Record an explicit indexing timestamp on each newly queued sidebar job
and expose bounded, sanitized stage percentiles so source, scan, queue, and
native-registration latency can be measured independently.

**Tech Stack:** Python 3.12, asyncio/threading, SQLite, Codex app-server JSON-RPC,
pytest through `scripts/run_tests.sh`, PowerShell service supervision.

---

## Invariants to preserve throughout

- A source session has at most one `session_sidebar_jobs` row and one exact
  `codex_thread_id`.
- Native creation remains `limit == 1`, reserve-before-create, and
  reconcile-after-ambiguity.
- Registration runtime overrides are process-local. They never rewrite
  `%USERPROFILE%\.codex\config.toml` or a task's stored normal configuration.
- Persistence verification uses a fresh, normal Codex app-server client.
- Terminal proof and readable hydration use a normal Codex app-server client,
  not the lean registration client.
- Existing sidebar delivery-state payloads remain decodable after upgrade.
- Automation-only, subagent-only, mirrored, empty, and source-cwd-less sessions
  keep their current exclusion behavior.
- Status exposes numbers and fixed codes only; no transcript text, paths,
  signed markers, tokens, or raw native task IDs.
- Do not modify the unrelated user change in
  `tests/session_bridge/test_claude_registrar.py`.

## Task 1: Define the lean registration app-server profile

**Files:**

- Create: `session_bridge/sidebar_runtime.py`
- Create: `tests/session_bridge/test_sidebar_runtime.py`

### Step 1: Write the failing pure-helper tests

- [ ] Add tests that require one immutable source of truth for the
  registration-only process arguments:

```python
def test_sidebar_registration_app_server_args_disable_mcp_and_plugins() -> None:
    assert sidebar_registration_app_server_args() == [
        "-c",
        "mcp_servers={}",
        "--disable",
        "plugins",
    ]


def test_sidebar_registration_app_server_args_returns_a_fresh_list() -> None:
    first = sidebar_registration_app_server_args()
    first.append("--unexpected")

    assert sidebar_registration_app_server_args() == [
        "-c",
        "mcp_servers={}",
        "--disable",
        "plugins",
    ]
```

- [ ] Add a hostile-input test proving the helper accepts no caller-supplied
  configuration, path, environment, model, or service-tier value.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_runtime.py -q
```

Expected: FAIL because `session_bridge.sidebar_runtime` does not exist.

### Step 2: Implement the narrow helper

- [ ] Add `session_bridge/sidebar_runtime.py` with a private tuple and a
  copy-returning function:

```python
_SIDEBAR_REGISTRATION_APP_SERVER_ARGS = (
    "-c",
    "mcp_servers={}",
    "--disable",
    "plugins",
)


def sidebar_registration_app_server_args() -> list[str]:
    """Return process-local Codex overrides for native sidebar registration."""

    return list(_SIDEBAR_REGISTRATION_APP_SERVER_ARGS)
```

- [ ] Do not add a `CODEX_HOME`, model, service tier, cwd, approval, sandbox, or
  filesystem override.

- [ ] Run the focused test again and expect PASS.

### Step 3: Commit the pure runtime profile

```powershell
git add session_bridge/sidebar_runtime.py tests/session_bridge/test_sidebar_runtime.py
git commit -m "feat(session-bridge): define lean sidebar registration runtime"
```

## Task 2: Isolate lean registration from normal verification and hydration

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`

### Step 1: Write failing composition and lifecycle tests

- [ ] In `tests/session_bridge/test_cli.py`, replace the single anonymous client
  capture in the focused production-backend tests with a recording factory:

```python
created: list[tuple[dict[str, object], ProtocolCodexClient]] = []


def client_factory(**kwargs: object) -> ProtocolCodexClient:
    client = ProtocolCodexClient(f"client-{len(created)}")
    created.append((dict(kwargs), client))
    return client
```

- [ ] Assert `_require_sidebar_executor()` creates:

  1. one long-lived registration client with
     `extra_args=sidebar_registration_app_server_args()`;
  2. no provider-scanning client;
  3. a fresh-client factory which, when invoked, creates a normal client with
     `codex_bin` only and no `extra_args`.

- [ ] Assert `_require_sidebar_terminal_delivery()` and
  `_require_sidebar_hydration_executor()` use a separate normal sidebar client.

- [ ] Assert `close()` and `_recycle_sidebar_delivery_runtime()` close and clear
  both sidebar clients exactly once, including partial-construction failure.

- [ ] In `tests/session_bridge/test_sidebar_executor.py`, extend the existing
  persisted-registration verification test to prove the fresh normal client:

  - initializes independently;
  - resumes the exact created thread with only `threadId`;
  - reads the exact signed registration marker;
  - is always closed;
  - does not invoke `thread/start` or `turn/start`.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_sidebar_executor.py -k "sidebar and (runtime or client or recycle or persisted_registration)" -q
```

Expected: FAIL because the backend currently shares one normal
`_sidebar_codex_client` for registration, proof, terminal reads, and hydration.

### Step 2: Split the production runtime ownership

- [ ] In `ProductionBackend.__init__`, keep `_sidebar_codex_client` as the normal
  read/hydration client and add:

```python
self._sidebar_registration_codex_client: CodexAppServerClient | None = None
```

- [ ] In `_require_sidebar_executor()`:

  - construct `_sidebar_registration_codex_client` with the helper's
    `extra_args`;
  - build `CodexSourceAdapter`, `SidebarThreadVerifier`, and the primary
    `CodexAppServerSidebarDelivery` around that lean client;
  - retain `fresh_client_factory=lambda: CodexAppServerClient(codex_bin=...)`
    with no lean overrides.

- [ ] Keep `_require_sidebar_terminal_delivery()` and
  `_require_sidebar_terminal_verifier()` on `_sidebar_codex_client`, constructed
  without `extra_args`.

- [ ] Update `close()` and `_recycle_sidebar_delivery_runtime()` to atomically
  detach, deduplicate, close, and clear both sidebar clients. Recycling a
  registration failure must not accidentally strand a normal hydration
  transport or reuse a possibly corrupted lean transport.

- [ ] Do not pass the lean profile through `thread/start`, `thread/resume`, or
  `turn/start` request `config` fields. The override belongs only on the app
  server's command line.

### Step 3: Run the focused tests

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_sidebar_executor.py -k "sidebar" -q
```

Expected: PASS.

### Step 4: Commit the runtime split

```powershell
git add session_bridge/cli.py tests/session_bridge/test_cli.py tests/session_bridge/test_sidebar_executor.py
git commit -m "fix(session-bridge): use lean native registration runtime"
```

## Task 3: Persist and report stage latency

**Files:**

- Modify: `hermes_state.py`
- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/store.py`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/mcp_server.py`
- Modify: `tests/session_bridge/test_store.py`
- Modify: `tests/session_bridge/test_coordinator.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_mcp_server.py`

### Step 1: Write failing schema and enqueue tests

- [ ] In `tests/session_bridge/test_store.py`, require
  `session_sidebar_jobs.indexed_at REAL` on new and upgraded databases.

- [ ] Create a legacy database fixture without `indexed_at`, reopen it through
  `SessionDB`, and assert declarative column reconciliation adds the nullable
  column without changing any existing sidebar job identity or state.

- [ ] Extend enqueue tests so a new job stores:

```python
assert row["eligible_at"] == 100.0
assert row["indexed_at"] == 106.0
assert row["created_at"] == 108.0
```

- [ ] Reject a boolean, NaN, infinity, negative value, or an `indexed_at`
  earlier than `eligible_at`. For legacy callers with no explicit
  `indexed_at`, use the enqueue clock clamped to at least `eligible_at`.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_store.py -k "sidebar and (indexed_at or latency or schema)" -q
```

Expected: FAIL because sidebar jobs do not have `indexed_at`.

### Step 2: Add the compatible timing column

- [ ] Add `indexed_at REAL` to the `session_sidebar_jobs` declaration in
  `BRIDGE_SCHEMA_SQL`. Keep it nullable so SQLite can add it to non-empty legacy
  tables through the existing declarative reconciler.

- [ ] Extend `SessionBridgeStore.enqueue_sidebar_job` with keyword-only
  `indexed_at: float | None = None`.

- [ ] Store the normalized value in the insert. Do not add `indexed_at` to
  `_SIDEBAR_DELIVERY_STATE_FIELDS`; existing signed delivery-state payloads
  must remain byte-for-byte decodable.

- [ ] For legacy rows, status queries derive
  `effective_indexed_at = COALESCE(indexed_at, created_at)`.

### Step 3: Capture the actual discovery boundary

- [ ] Add `indexed_at: float | None = None` to `SidebarSource`, not
  `SidebarCandidate`.

- [ ] In `SessionBridgeStore.list_sidebar_candidates()`:

  - select `external_sessions.last_indexed_at` for native Claude rows;
  - return `None` for central and profile Hermes rows because their bridge
    discovery boundary is the current registration cycle;
  - validate any non-null value as finite.

- [ ] In `_register_sidebar_jobs_locked()`, compute:

```python
indexed_at = (
    source.indexed_at
    if source.indexed_at is not None
    else registration_time
)
indexed_at = max(projection.last_active, indexed_at)
```

- [ ] Pass `indexed_at` to `enqueue_sidebar_job`. Keep the existing newest probe,
  durable catch-up cursor, per-candidate exception isolation, and
  `continuous_batch_limit`.

- [ ] Add coordinator tests proving:

  - a Claude candidate preserves its catalog indexing timestamp;
  - a profile Hermes candidate uses the registration-cycle timestamp;
  - the newest meaningful profile session is queued before historical catch-up
    candidates;
  - empty, cron, and subagent profile sessions remain excluded.

### Step 4: Compute bounded stage percentiles

- [ ] In `SessionBridgeStore.sidebar_delivery_status()`, use the existing recent
  visible sample limit and compute non-negative values for:

```text
source_to_index = effective_indexed_at - eligible_at
index_to_queue = created_at - effective_indexed_at
queue_to_visible = visible_at - created_at
source_to_visible = visible_at - eligible_at
```

- [ ] Return:

```python
"stage_latency_seconds": {
    "source_to_index": {"p50": ..., "p95": ...},
    "index_to_queue": {"p50": ..., "p95": ...},
    "queue_to_visible": {"p50": ..., "p95": ...},
    "source_to_visible": {"p50": ..., "p95": ...},
}
```

- [ ] Preserve the existing `delivery_latency_seconds` p50/p95/p99 contract as
  an end-to-end compatibility alias.

- [ ] Extend the strict CLI and MCP status shapers to accept only the four fixed
  stage names and the fixed `p50`/`p95` keys. Unknown mappings, strings, hostile
  objects, NaN, and infinity must become `None` or fail the existing internal
  schema boundary; they must never be stringified.

- [ ] Update `tests/session_bridge/test_cli.py` and
  `tests/session_bridge/test_mcp_server.py` to assert exact sanitized output and
  absence of transcript text, paths, markers, and raw task IDs.

### Step 5: Run the timing and status tests

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_store.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py -k "sidebar and (indexed_at or stage_latency or profile or status)" -q
```

Expected: PASS.

### Step 6: Commit stage observability

```powershell
git add hermes_state.py session_bridge/sidebar.py session_bridge/store.py session_bridge/coordinator.py session_bridge/cli.py session_bridge/mcp_server.py tests/session_bridge/test_store.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): expose sidebar stage latency"
```

## Task 4: Lock in source-isolation and priority regressions

**Files:**

- Modify: `tests/session_bridge/test_coordinator.py`
- Modify only if a regression test exposes a defect:
  `session_bridge/coordinator.py`

### Step 1: Add the failing-transcript regression

- [ ] Add one persistent Claude scan test with three changed transcript IDs:
  newest succeeds, middle parse raises a fixed exception, oldest succeeds.

- [ ] Assert the same scan:

  - indexes both valid transcripts;
  - reports exactly one failure;
  - leaves only the failed native ID in `_CLAUDE_PENDING_KEY`;
  - commits fingerprints only for successful IDs;
  - marks Claude degraded with the fixed `claude_scan_failed` code;
  - exposes no exception message or native path in health/status.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py -k "claude and scan and isolate" -q
```

Expected: PASS on the current implementation. If it fails, make the smallest
change inside `_scan_claude_persistent()` that preserves the failed ID and
continues the loop.

### Step 2: Strengthen newest-before-catch-up coverage

- [ ] Extend the existing newest-probe tests to insert a meaningful Hermes
  profile source after a durable catch-up cursor already exists.

- [ ] Assert one registration cycle sees and queues the new profile source
  before resuming historical catch-up, and the next cycle resumes the exact
  durable cursor without duplication.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py -k "sidebar_registration and (newest or profile or catchup)" -q
```

Expected: PASS. Change production code only if the test demonstrates a real
ordering defect.

### Step 3: Commit the regression proof

```powershell
git add tests/session_bridge/test_coordinator.py
git diff --cached --quiet session_bridge/coordinator.py; if ($LASTEXITCODE -ne 0) { git add session_bridge/coordinator.py }
git commit -m "test(session-bridge): preserve fresh-source discovery under failures"
```

## Task 5: Verify normal resume semantics and lean process behavior

**Files:**

- Modify only if needed: `tests/session_bridge/test_sidebar_executor.py`
- Create: `scripts/verify_sidebar_registration_runtime.py`

### Step 1: Write a non-mutating verifier mode first

- [ ] Create a script with default read-only behavior. Without `--apply`, it
  prints the exact Codex commands it will use and exits without creating a task.

- [ ] Require all mutating runs to include:

```text
--apply --confirm CREATE_ONE_DISPOSABLE_SIDEBAR_RUNTIME_PROBE
```

- [ ] The apply path must:

  1. initialize a normal app-server client and capture sanitized
     `config/read` output;
  2. initialize a lean client with
     `sidebar_registration_app_server_args()`;
  3. assert the lean runtime's `mcpServerStatus/list` reports no configured
     server startups;
  4. create one uniquely titled disposable task with a signed sidebar marker;
  5. wait for the exact `REGISTERED` acknowledgement;
  6. close the lean client;
  7. resume the exact task through a fresh normal client with only `threadId`;
  8. compare the resumed task's sanitized effective configuration with the
     normal baseline for MCP/plugin availability;
  9. archive only the exact disposable task after identity and marker proof;
  10. print elapsed create, acknowledgement, resume, and total seconds.

- [ ] Never delete tasks or mutate `%USERPROFILE%\.codex\config.toml`.

### Step 2: Unit-test argument and safety gates

- [ ] Test the script's pure argument parser, confirmation gate, sanitized
  comparison, exact-task archive gate, and failure cleanup with fake clients.

- [ ] Run:

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_sidebar_runtime.py -k "runtime or normal_resume or confirmation" -q
```

Expected: PASS.

### Step 3: Run the explicit local characterization

```powershell
.\.venv\Scripts\python.exe scripts\verify_sidebar_registration_runtime.py
.\.venv\Scripts\python.exe scripts\verify_sidebar_registration_runtime.py --apply --confirm CREATE_ONE_DISPOSABLE_SIDEBAR_RUNTIME_PROBE
```

Acceptance:

- lean runtime reports zero configured MCP startups;
- normal resume matches the normal baseline;
- one exact disposable task is archived after proof;
- no duplicate task is created;
- total registration is below 15 seconds on the current workstation.

### Step 4: Commit the verifier

```powershell
git add scripts/verify_sidebar_registration_runtime.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_sidebar_runtime.py
git commit -m "test(session-bridge): verify lean registration handoff"
```

## Task 6: Full verification, rollout, and latency canary

**Files:**

- Verify only: all modified files
- Runtime state: `C:\Users\diego\.hermes\session-bridge`

### Step 1: Run the focused session-bridge suite

```powershell
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_runtime.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_store.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py -q
```

Expected: PASS.

### Step 2: Run the complete session-bridge suite

```powershell
bash scripts/run_tests.sh tests/session_bridge/ -q
```

Expected: PASS with no test reading or writing the real
`C:\Users\diego\.hermes\state.db`.

### Step 3: Review the final diff and user-owned changes

```powershell
git status --short
git diff --check
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- session_bridge hermes_state.py scripts tests/session_bridge
```

- [ ] Confirm `tests/session_bridge/test_claude_registrar.py` contains only the
  pre-existing user change and is absent from every implementation commit.

- [ ] Confirm no generated schema, temporary task transcript, PID, log, auth
  file, or local Codex configuration is staged.

### Step 4: Restart only the authenticated service tree

- [ ] Read
  `C:\Users\diego\.hermes\session-bridge\service.pid`, validate that its PID,
  executable path, creation time, agent root, and command match the same checks
  in `launch-session-bridge.ps1`, then stop only that exact recorded `uv`
  process tree.

- [ ] Start the existing supervisor:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\.hermes\session-bridge\launch-session-bridge.ps1
```

- [ ] Run the existing health smoke:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\diego\.hermes\session-bridge\session_bridge_smoke.ps1 -TimeoutSec 5 -Component SessionBridge -AgentRoot C:\Users\diego\.hermes\agent-src -UvPath C:\Users\diego\.local\bin\uv.exe
```

Expected: exit 0. If identity validation fails, do not kill any process; stop
and report the exact mismatch.

### Step 5: Run a no-backlog live canary

- [ ] Start one meaningful Claude Code Desktop session and one meaningful
  non-cron Hermes profile session, recording their first persisted user-message
  timestamps.

- [ ] Poll sanitized status only:

```powershell
.\.venv\Scripts\python.exe -m session_bridge.cli sidebar-status
```

- [ ] Verify each source maps to one exact new Codex task and that recent
  `stage_latency_seconds.source_to_visible.p95` is at most 30 seconds.

- [ ] Open each imported task in the normal Codex app and verify the readable
  summary and last five messages are present and a normal follow-up can use the
  user's configured capabilities.

### Step 6: Run the ten-session burst gate

- [ ] Generate ten meaningful, non-automation test sources through the existing
  test fixtures or disposable profile database; do not write synthetic rows to
  the live database.

- [ ] Record the first source timestamp and the tenth visible task timestamp.
  Require:

  - total drain time at most 180 seconds;
  - exactly ten distinct source IDs and ten distinct Codex task IDs;
  - zero new `native_create_ambiguous` failures;
  - zero duplicate bridge IDs;
  - queue-to-visible p95 within the measured single-writer budget.

- [ ] If the burst misses 180 seconds after lean registration is proven, stop.
  Do not add concurrency in this change; capture the measurement for a separate
  reviewed design.

### Step 7: Record the result in shared memory

- [ ] Search GBrain and MemPalace for the current session-bridge page/drawer.
- [ ] Add one MemPalace record in wing `session-bridge`, room `rollouts`, with
  commit IDs, test commands, canary timings, burst timings, and rollback notes.
- [ ] Add a GBrain timeline entry to
  `systems/cross-harness-session-bridge` summarizing the verified latency result
  without secrets, task IDs, or transcript content.

### Step 8: Final completion gate

Do not claim the delay is fixed unless all of these are true:

- [ ] focused and complete session-bridge suites pass;
- [ ] lean runtime starts no configured MCP servers;
- [ ] a lean-created task resumes with normal Codex configuration;
- [ ] one Claude and one Hermes live canary are visible within 30 seconds;
- [ ] ten sessions drain within three minutes;
- [ ] no duplicate or replacement task is created;
- [ ] stage timing is visible and sanitized;
- [ ] the service smoke passes after restart.
