# Session Inbox Placement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create every new Claude and Hermes sidebar import under the saved
local `.hermes` Session Inbox while preserving the exact source cwd and
worktree as authenticated continuation identity.

**Architecture:** Add an immutable placement object alongside the existing
`SidebarCandidate`; never overwrite `SidebarCandidate.cwd`. Resolve and validate
the inbox before native dispatch, start Codex threads with the inbox as `cwd`
and both inbox/source paths as runtime roots, and verify native placement plus
the source cwd embedded in the authenticated registration. Keep the existing
single-writer, reserve-before-create, exact-ID bind, fresh-client persistence
proof, and no-blind-retry flow unchanged.

**Tech Stack:** Python 3.12, dataclasses, pathlib/Windows path normalization,
Codex app-server JSON-RPC v2, SQLite, pytest through `scripts/run_tests.sh`.

---

## Dependencies and invariants

- Execute this plan before
  `2026-07-30-projectless-session-recovery.md` and
  `2026-07-30-session-readable-refresh.md`.
- `SidebarCandidate.cwd` always means the exact source cwd.
- `SidebarPlacement.inbox_cwd` always means presentation/project placement.
- Native creation remains one-at-a-time and reservation-backed.
- A missing or invalid inbox fails before `thread/start`.
- Once `thread/start` is invoked, uncertainty remains
  `native_create_ambiguous`; it never authorizes replacement creation.
- Persistence verification still uses a fresh normal Codex runtime, not the
  lean registration process.
- No code writes Codex state, rollout files, saved projects, or task metadata
  outside supported native APIs.

## File map

- Create `session_bridge/sidebar_placement.py`: immutable placement model,
  canonical path validation, runtime-root construction.
- Modify `session_bridge/config.py`: parse placement settings.
- Modify `hermes_cli/config.py`: installation defaults.
- Modify `hermes_state.py`: persist verified placement generation/timestamp.
- Modify `session_bridge/sidebar_executor.py`: pass and verify placement
  separately from source identity.
- Modify `session_bridge/codex_adapter.py`: recovery-key verification against
  placement cwd.
- Modify `session_bridge/cli.py`: resolve placement once during executor
  composition and expose sanitized placement status.
- Modify `session_bridge/mcp_server.py`: include sanitized placement health.
- Create `tests/session_bridge/test_sidebar_placement.py`.
- Modify `tests/session_bridge/test_config_safety.py`.
- Modify `tests/session_bridge/test_sidebar_executor.py`.
- Modify `tests/session_bridge/test_target_adapters.py`.
- Modify `tests/session_bridge/test_cli.py`.
- Modify `tests/session_bridge/test_mcp_server.py`.
- Modify `tests/test_hermes_state.py`.
- Modify `tests/hermes_state/test_session_bridge_schema.py`.

## Task 1: Define and validate placement configuration

**Files:**

- Create: `session_bridge/sidebar_placement.py`
- Create: `tests/session_bridge/test_sidebar_placement.py`
- Modify: `session_bridge/config.py`
- Modify: `hermes_cli/config.py`
- Modify: `tests/session_bridge/test_config_safety.py`

- [ ] **Step 1: Write failing model and path-validation tests**

Add tests for the exact public contract:

```python
def test_resolve_sidebar_placement_keeps_source_out_of_identity(tmp_path: Path) -> None:
    inbox = tmp_path / ".hermes"
    source = tmp_path / "repo" / ".claude" / "worktrees" / "task"
    inbox.mkdir()
    source.mkdir(parents=True)

    placement = resolve_sidebar_placement(
        configured_inbox_cwd=str(inbox),
        hermes_home=inbox,
        placement_generation=1,
        source_cwd=str(source),
    )

    assert placement.inbox_cwd == str(inbox.resolve())
    assert placement.local_host == "local"
    assert placement.placement_generation == 1
    assert placement.runtime_workspace_roots == (
        str(inbox.resolve()),
        str(source.resolve()),
    )
```

Also require:

- duplicate inbox/source roots collapse to one root;
- relative, nonexistent, file-valued, non-canonical, or differently configured
  inbox paths raise `SidebarPlacementError("inbox_unavailable")`;
- a missing/non-absolute source raises
  `SidebarPlacementError("source_identity_mismatch")`;
- a boolean or generation other than exactly `1` is rejected;
- Windows case and separator equivalents compare equal without changing the
  canonical returned spelling.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_placement.py tests/session_bridge/test_config_safety.py -q'
```

Expected: FAIL because `session_bridge.sidebar_placement` and the new config
fields do not exist.

- [ ] **Step 2: Implement the immutable placement boundary**

Create:

```python
from dataclasses import dataclass
from pathlib import Path


class SidebarPlacementError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {"inbox_unavailable", "source_identity_mismatch"}:
            raise ValueError("sidebar placement error code is not fixed")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SidebarPlacement:
    inbox_cwd: str
    local_host: str
    runtime_workspace_roots: tuple[str] | tuple[str, str]
    placement_generation: int
```

Implement `resolve_sidebar_placement` with `Path.resolve(strict=True)`.
Require the configured inbox and profile-safe Hermes home to be filesystem
equivalent. Return roots in `(inbox, source)` order and deduplicate by
case-insensitive normalized Windows identity.

- [ ] **Step 3: Add strict config fields**

Extend `SidebarConfig`:

```python
inbox_cwd: str | None = None
placement_generation: int = 1
```

Add only `inbox_cwd` and `placement_generation` to the sidebar TOML allowlist;
do not add environment variables. Parse `inbox_cwd` as an exact nonempty
string when supplied, and require `placement_generation == 1`.

Set the installed Hermes default in `hermes_cli/config.py` to the profile-safe
Hermes home string and generation `1`. Tests must inject temporary paths rather
than reading the real home directory.

- [ ] **Step 4: Run tests and commit**

Expected: focused tests PASS.

```powershell
git add session_bridge/sidebar_placement.py session_bridge/config.py hermes_cli/config.py tests/session_bridge/test_sidebar_placement.py tests/session_bridge/test_config_safety.py
git commit -m "feat(session-bridge): define canonical sidebar placement"
```

## Task 2: Start native tasks in the inbox without losing source cwd

**Files:**

- Modify: `session_bridge/sidebar_executor.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`

- [ ] **Step 1: Write the failing native request tests**

Update the current `thread/start` test so the candidate remains
`cwd="C:/source"` while the placement is `C:/Users/diego/.hermes`:

```python
created = delivery.create_thread(
    prompt="registration",
    candidate=candidate,
    placement=SidebarPlacement(
        inbox_cwd="C:/Users/diego/.hermes",
        local_host="local",
        runtime_workspace_roots=(
            "C:/Users/diego/.hermes",
            "C:/source",
        ),
        placement_generation=1,
    ),
    recovery_key=RECOVERY_KEY,
    deadline=10.0,
)

assert client.calls[0] == (
    "thread/start",
    {
        "cwd": "C:/Users/diego/.hermes",
        "runtimeWorkspaceRoots": [
            "C:/Users/diego/.hermes",
            "C:/source",
        ],
        "threadSource": RECOVERY_KEY,
    },
)
assert created == THREAD_1
```

Add failures for returned cwd equal to the source rather than inbox, malformed
runtime roots, and placement validation failure. Assert all validation failures
occur before `client.request`.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_executor.py -k "create_thread or placement or runtime_workspace" -q'
```

Expected: FAIL because `create_thread` has no placement argument and still
uses `candidate.cwd`.

- [ ] **Step 2: Extend only the narrow native boundary**

Change the protocol signature to:

```python
def create_thread(
    self,
    *,
    prompt: str,
    candidate: SidebarCandidate,
    placement: SidebarPlacement,
    recovery_key: str,
    deadline: float,
) -> str:
    raise NotImplementedError
```

Send:

```python
{
    "cwd": placement.inbox_cwd,
    "runtimeWorkspaceRoots": list(placement.runtime_workspace_roots),
    "threadSource": expected_recovery_key,
}
```

Validate the returned thread ID, recovery key, and returned cwd against
`placement.inbox_cwd`. Do not compare returned cwd to `candidate.cwd`.

- [ ] **Step 3: Make placement a required executor dependency**

Add:

```python
placement_resolver: Callable[[SidebarCandidate], SidebarPlacement]
```

to `SidebarExecutor.__init__`. Resolve placement after loading/authenticating
the candidate and before reading or creating a reservation. Map
`SidebarPlacementError.code` directly to a fixed executor failure. Pass the
placement through creation, reserved-thread recovery, idle polling, and final
verification.

Extend the fixed allowlists exactly:

```python
SIDEBAR_RETRYABLE_ERRORS = SIDEBAR_RETRYABLE_ERRORS | {"inbox_unavailable"}
SIDEBAR_FATAL_ERRORS = SIDEBAR_FATAL_ERRORS | {"placement_mismatch"}
```

`inbox_unavailable` is definite and pre-dispatch. `placement_mismatch` means a
native task identity exists in the wrong placement and therefore cannot
authorize replacement creation.

Replace each `expected_cwd=candidate.cwd` recovery/poll call with
`expected_cwd=placement.inbox_cwd`. Keep registration prompt construction
unchanged so its signed metadata still says `Source cwd: "<candidate.cwd>"`.

- [ ] **Step 4: Run tests and commit**

```powershell
git add session_bridge/sidebar_executor.py tests/session_bridge/test_sidebar_executor.py
git commit -m "fix(session-bridge): create imports in session inbox"
```

## Task 3: Verify both placement identity and authenticated source identity

**Files:**

- Modify: `session_bridge/sidebar_executor.py`
- Modify: `session_bridge/codex_adapter.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`
- Modify: `tests/session_bridge/test_target_adapters.py`

- [ ] **Step 1: Write failing verification tests**

Require these independent failures:

1. native thread cwd differs from the inbox:
   `placement_mismatch`;
2. registration marker is authentic but its `Source cwd:` line differs from
   `candidate.cwd`: `source_identity_mismatch`;
3. recovery-key inventory finds a task in source cwd rather than inbox:
   `codex_thread_conflict`;
4. exact inbox cwd, exact source line, exact marker, completed turn, and fresh
   persistence proof succeed.

Run:

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py -k "placement or source_cwd or recovery_key" -q'
```

Expected: at least the placement/source separation cases FAIL.

- [ ] **Step 2: Add exact registration metadata parsing**

In `sidebar.py`, add a pure authenticated helper:

```python
@dataclass(frozen=True)
class SidebarRegistrationIdentity:
    source_session_id: str
    source_cwd: str
    bridge_id: str


def decode_sidebar_registration_identity(
    prompt: object,
    marker_secret: bytes,
) -> SidebarRegistrationIdentity:
    raise NotImplementedError
```

Reuse the exact structural checks from `classify_sidebar_initial_prompt`;
decode the JSON `Source session ID:` and `Source cwd:` lines; authenticate the
existing signed bridge marker; reject extra/missing lines and noncanonical
values.

- [ ] **Step 3: Enforce the two identities in the executor**

Before final commit:

```python
initial_prompt = self._native.read_thread_initial_prompt(
    thread_id=thread_id,
    deadline=operation_deadline,
)
identity = decode_sidebar_registration_identity(
    initial_prompt,
    self._marker_secret,
)
if (
    identity.source_session_id != candidate.source_session_id
    or not _filesystem_equivalent(identity.source_cwd, candidate.cwd)
):
    return self._settle(
        job_id=job_id,
        lease_token=lease_token,
        thread_id=thread_id,
        error_code="source_identity_mismatch",
    )
```

Call `_wait_until_idle` with
`expected_cwd=placement.inbox_cwd`. Add
`placement_mismatch` to the fixed fatal/retry policy chosen by the approved
spec; it must never be rendered with raw paths.

- [ ] **Step 4: Run tests and commit**

```powershell
git add session_bridge/sidebar.py session_bridge/sidebar_executor.py session_bridge/codex_adapter.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py
git commit -m "fix(session-bridge): verify inbox and source identities"
```

## Task 4: Compose placement and expose sanitized health

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/mcp_server.py`
- Modify: `hermes_state.py`
- Modify: `session_bridge/store.py`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_mcp_server.py`
- Modify: `tests/session_bridge/test_store.py`
- Modify: `tests/test_hermes_state.py`
- Modify: `tests/hermes_state/test_session_bridge_schema.py`

- [ ] **Step 1: Write failing composition/status tests**

Assert `_require_sidebar_executor()` supplies a resolver that:

- reads the profile-safe Hermes home;
- compares it to configured `inbox_cwd`;
- uses each exact candidate source cwd;
- never mutates the candidate;
- fails with `inbox_unavailable` before native dispatch.

Require sanitized status:

```python
assert status["placement"] == {
    "inbox_cwd": "C:\\Users\\diego\\.hermes",
    "generation": 1,
    "verified_visible": 12,
    "mismatch_count": 0,
    "canary": {"status": "passed", "verified_at": 1234.0},
}
```

Unknown keys, non-finite counts, raw source cwd, markers, tokens, and task IDs
must be rejected or omitted.

- [ ] **Step 2: Wire the resolver**

Construct:

```python
def resolve(candidate: SidebarCandidate) -> SidebarPlacement:
    return resolve_sidebar_placement(
        configured_inbox_cwd=self.config.sidebar.inbox_cwd,
        hermes_home=get_hermes_home(),
        placement_generation=self.config.sidebar.placement_generation,
        source_cwd=candidate.cwd,
    )
```

Pass it to `SidebarExecutor`. Add placement summary fields to
`sidebar_delivery_status`, `_public_sidebar_status`, and MCP health shaping.
Count a task as generation-1 placement success only after the executor has
verified inbox cwd and committed visibility.

- [ ] **Step 3: Persist verified placement generation**

Add nullable, upgrade-safe columns to `session_sidebar_jobs`:

```text
placement_generation INTEGER
placement_verified_at REAL
```

Bump `SCHEMA_VERSION` once. Extend `commit_sidebar_job_with_lineage` with
`placement_generation: int`; write both fields in the same transaction as the
visible state and canonical link. Exact completion replay must require the
same generation. Existing visible rows remain null and therefore count as
awaiting recovery rather than verified placement.

Persist the real-canary result as a versioned `session_bridge_state` record
containing only status, placement generation, verified timestamp, and a digest
of the canary identity. The public shaper exposes status/time but not the
digest or task ID.

- [ ] **Step 4: Run focused status tests**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_store.py -k "sidebar and placement" -q'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add hermes_state.py session_bridge/cli.py session_bridge/mcp_server.py session_bridge/store.py tests/test_hermes_state.py tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): report inbox placement health"
```

## Task 5: Prove placement with tests and one real canary

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Modify: `tests/session_bridge/test_sidebar_executor.py`
- Verify only: production runtime

- [ ] **Step 1: Add an end-to-end source/inbox separation case**

Create a source under a disposable transient worktree and a saved-project inbox
at a different path. Assert:

- the native start request uses inbox cwd;
- runtime roots contain inbox and source;
- the readable prompt contains the exact source cwd;
- the exact task is committed once;
- one mirror link exists;
- no second create happens after a simulated post-dispatch restart.

- [ ] **Step 2: Run the focused and complete Session Bridge suites**

```powershell
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/test_sidebar_placement.py tests/session_bridge/test_sidebar_executor.py tests/session_bridge/test_target_adapters.py tests/session_bridge/test_cli.py tests/session_bridge/test_mcp_server.py tests/session_bridge/test_end_to_end.py -q'
& 'C:\Program Files\Git\bin\bash.exe' -lc 'cd /c/Users/diego/.hermes/agent-src/.worktrees/session-inbox-placement-recovery && export HERMES_PYTHON=/c/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe && scripts/run_tests.sh tests/session_bridge/ -q --file-timeout 900'
```

Expected: PASS.

- [ ] **Step 3: Run the production gate**

With recovery and refresh still disabled:

1. verify Codex reports the saved `.hermes` project and record its exact
   `projectId`;
2. create one meaningful disposable Claude source outside `.hermes`;
3. allow exactly one bridge import;
4. read the returned task through the normal native API;
5. assert its `projectId` equals the recorded `.hermes` project ID, its task cwd
   equals `.hermes`, its readable preview shows the exact source cwd and last
   five messages, and its canonical link is unique;
6. archive only the disposable canary after exact marker and source proof.

If `projectId` is null or differs, disable continuous new placement and stop.
Do not proceed to either later implementation plan's live rollout.

- [ ] **Step 4: Commit the end-to-end proof**

```powershell
git add tests/session_bridge/test_end_to_end.py tests/session_bridge/test_sidebar_executor.py
git commit -m "test(session-bridge): prove session inbox placement"
```

## Completion gate

- [ ] New imports start with inbox cwd and both runtime roots.
- [ ] Source cwd remains authenticated and unchanged.
- [ ] Reserved/recovered native lookup uses inbox cwd.
- [ ] Fresh normal-client persistence proof still passes.
- [ ] Status distinguishes verified placement from mere native existence.
- [ ] Focused and complete suites pass.
- [ ] The real canary has the saved `.hermes` project ID.
- [ ] No production import after cutover has `projectId: null`.
