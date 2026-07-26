# Session Sidebar Readable Hydration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace metadata-only Codex sidebar imports with a redacted continuation brief plus the last five user/assistant messages, and hydrate existing placeholder-only tasks in place without replacement or blind resend.

**Architecture:** Add a side-effect-free preview builder over indexed Session Bridge data, embed its deterministic output in new registration prompts, and add a separate durable hydration queue for legacy visible tasks. The broker continues to authenticate exact native task identity and signed markers; hydration sends are reserved before dispatch and reconciled by an exact signed hydration marker after ambiguity. `session_continue` remains the only context-pack freeze and lineage transition.

**Tech Stack:** Python 3.11, SQLite through `SessionDB`, FastMCP, pytest/pytest-asyncio, Codex native task tools, TOML configuration, Markdown skill assets.

---

## Scope and file map

The implementation is one product change with two independently testable delivery
paths:

1. readable prompts for newly created native Codex tasks;
2. exact-task hydration for already-visible legacy placeholders.

The second path depends on preview generation but never re-enters native creation.

**Create:**

- `session_bridge/preview.py` — immutable preview value objects, message selection,
  bounded rendering, digesting, and source-snapshot validation.
- `tests/session_bridge/test_preview.py` — pure preview and indexed-snapshot tests.

**Modify:**

- `hermes_state.py` — add the durable hydration queue and indexes.
- `session_bridge/models.py` — add hydration states and public preview dataclasses.
- `session_bridge/context_pack.py` — expose the existing deterministic extraction
  primitives needed by the preview builder without changing pack persistence.
- `session_bridge/sidebar.py` — render and classify legacy/readable registration
  prompts and sign hydration markers.
- `session_bridge/store.py` — preview snapshot reads plus hydration seed, claim,
  reserve, commit, fail, status, and reconciliation transitions.
- `session_bridge/coordinator.py` — build previews, claim hydration work, and
  validate exact source/task identity.
- `session_bridge/mcp_server.py` — return readable registration prompts and expose
  bounded hydration queue tools.
- `session_bridge/broker_client.py` — authenticated loopback parity for hydration
  operations.
- `session_bridge/config.py` — disabled-by-default readable-preview and legacy
  hydration rollout gates.
- `session_bridge/cli.py` — guarded canary seeding, status, and feature-gate
  commands.
- `session_bridge/assets/session-sidebar-sync/SKILL.md` — process either one
  registration lease or one hydration lease, never both, and use exact-task
  `send_message_to_thread` reconciliation.
- `tests/session_bridge/test_context_pack.py`
- `tests/session_bridge/test_sidebar.py`
- `tests/session_bridge/test_store.py`
- `tests/session_bridge/test_coordinator.py`
- `tests/session_bridge/test_mcp_server.py`
- `tests/session_bridge/test_broker_client.py`
- `tests/session_bridge/test_cli.py`
- `tests/session_bridge/test_sidebar_skill.py`
- `tests/session_bridge/test_end_to_end.py`
- `docs/superpowers/specs/2026-07-26-session-sidebar-readable-hydration-design.md`
  — record final API names if implementation review changes them.

**Operational constraint:** The deleted `session-sidebar-sync-worker` heartbeat must
not be recreated. Development and canary runs are manual. Any future background
schedule requires separate user approval and must not attach recurring wakes to an
interactive work task.

### Task 1: Add pure preview selection and rendering

**Files:**

- Create: `session_bridge/preview.py`
- Create: `tests/session_bridge/test_preview.py`
- Modify: `session_bridge/models.py`
- Modify: `session_bridge/context_pack.py`
- Test: `tests/session_bridge/test_context_pack.py`

- [ ] **Step 1: Write failing tests for the public preview contract**

Add these cases to `tests/session_bridge/test_preview.py`:

```python
from session_bridge.models import PreviewMessage, SessionPreview
from session_bridge.preview import build_session_preview


def test_preview_selects_last_five_conversational_messages_in_order() -> None:
    messages = [
        {"id": index, "role": role, "content": content, "timestamp": float(index)}
        for index, (role, content) in enumerate([
            ("system", "system"),
            ("user", "one"),
            ("assistant", "two"),
            ("tool", "tool output"),
            ("user", "three"),
            ("assistant", "four"),
            ("user", "five"),
            ("assistant", "six"),
        ])
    ]

    preview = build_session_preview(
        source_session_id="claude:source",
        source_cursor="cursor-1",
        source_hash="hash-1",
        title="Readable source",
        provider="claude",
        cwd=r"C:\repo",
        captured_at=8.0,
        messages=messages,
        git_root=r"C:\repo",
        git_branch="main",
        git_head="abc123",
        worktree_id="worktree:v1:test",
        budget_chars=24_000,
    )

    assert [(item.role, item.content) for item in preview.recent_messages] == [
        ("assistant", "two"),
        ("user", "three"),
        ("assistant", "four"),
        ("user", "five"),
        ("assistant", "six"),
    ]
    assert preview.rendered.startswith("# Imported Claude Code Session")
    assert len(preview.rendered) <= 24_000
```

Also add explicit tests that exclude `REGISTERED`, internal bridge events, tool
calls/results, inactive messages, empty text, and messages emptied by redaction.
Assert timestamps are preserved, secrets become `[REDACTED]`, oversized messages
contain `[truncated]`, and identical inputs produce identical SHA-256
digests.

- [ ] **Step 2: Run the preview tests and verify the red state**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_preview.py -q
```

Expected: collection fails because `session_bridge.preview`,
`PreviewMessage`, and `SessionPreview` do not exist.

- [ ] **Step 3: Add immutable preview models**

Add to `session_bridge/models.py`:

```python
@dataclass(frozen=True)
class PreviewMessage:
    role: str
    content: str
    timestamp: float | None
    truncated: bool = False


@dataclass(frozen=True)
class SessionPreview:
    version: int
    source_session_id: str
    source_cursor: str
    source_hash: str
    captured_at: float
    recent_messages: Sequence[PreviewMessage]
    rendered: str
    digest: str
    budget_chars: int
    truncated: bool
```

Keep these presentation values separate from `ContextPack`; a preview is never
persisted as or coerced into a continuation pack.

- [ ] **Step 4: Expose deterministic extraction without pack persistence**

In `session_bridge/context_pack.py`, lift the existing goal/decision/open-work/file
classification into this pure function and make `ContextPackBuilder._extract_items`
call it:

```python
def extract_context_sections(
    messages: Sequence[Mapping[str, Any]],
) -> Mapping[str, Sequence[str]]:
    """Return redacted deterministic brief items without reading or writing state."""
```

The returned keys are exactly:

```python
(
    "Goal / Latest Intent",
    "Decisions and Constraints",
    "Unresolved Work",
    "Files",
    "Referenced MemPalace / GBrain Links",
)
```

Preserve the existing ordering, duplicate suppression, and `_redact` behavior so
the context-pack regression tests remain byte-for-byte stable.

- [ ] **Step 5: Implement bounded preview rendering**

Create `session_bridge/preview.py` with:

```python
PREVIEW_VERSION = 1
DEFAULT_PREVIEW_BUDGET_CHARS = 24_000
RECENT_MESSAGE_LIMIT = 5


def build_session_preview(
    *,
    source_session_id: str,
    source_cursor: str,
    source_hash: str,
    title: str | None,
    provider: str,
    cwd: str,
    captured_at: float,
    messages: Sequence[Mapping[str, Any]],
    git_root: str | None,
    git_branch: str | None,
    git_head: str | None,
    worktree_id: str | None,
    budget_chars: int = DEFAULT_PREVIEW_BUDGET_CHARS,
) -> SessionPreview:
```

Implementation rules:

- validate canonical non-empty identity strings and a budget in `1..100_000`;
- select active `user`/`assistant` messages only;
- reject exact legacy/readable registration blocks and `REGISTERED`;
- redact before selection and size accounting;
- take the last five qualifying messages, then render them chronologically;
- render identity, goal, unresolved work, decisions, five messages, and repository
  metadata in that priority;
- use a fence longer than the longest backtick run in imported text;
- calculate `digest = sha256(rendered.encode("utf-8")).hexdigest()`;
- never run git, touch the filesystem, or write the database.

- [ ] **Step 6: Run preview and context-pack tests**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_preview.py tests\session_bridge\test_context_pack.py -q
```

Expected: all tests pass and existing context-pack payload assertions remain
unchanged.

- [ ] **Step 7: Commit the pure preview layer**

```powershell
git add session_bridge\models.py session_bridge\context_pack.py session_bridge\preview.py tests\session_bridge\test_preview.py tests\session_bridge\test_context_pack.py
git commit -m "feat(session-bridge): build bounded readable previews"
```

### Task 2: Read indexed snapshots and render readable registration prompts

**Files:**

- Modify: `session_bridge/config.py`
- Modify: `session_bridge/store.py`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/mcp_server.py`
- Test: `tests/session_bridge/test_preview.py`
- Test: `tests/session_bridge/test_sidebar.py`
- Test: `tests/session_bridge/test_mcp_server.py`
- Test: `tests/session_bridge/test_coordinator.py`

- [ ] **Step 1: Write failing indexed-snapshot and readable-prompt tests**

Add tests proving:

```python
preview = store.get_sidebar_preview_source("claude:source")
assert preview["source_cursor"] == "cursor-1"
assert preview["source_hash"] == "hash-1"
assert [message["content"] for message in preview["messages"]][-1] == "latest"
```

And:

```python
prompt = build_registration_prompt(candidate, marker, preview=preview)
assert prompt.index("# Imported Claude Code Session") < prompt.index(
    "## Bridge Registration"
)
assert "## Continuation Brief" in prompt
assert "## Last 5 Messages" in prompt
assert f"Signed marker: {marker}" in prompt
assert prompt.endswith("Until that later user message, reply with only: REGISTERED")
```

Update the existing negative transcript test so it asserts the leased response
contains only the redacted bounded preview, never a sixth message, provider-native
path, tool output, or injected secret.

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_sidebar.py tests\session_bridge\test_mcp_server.py -k "registration_prompt or pending_never_reads_transcript or readable_preview" -q
```

Expected: failures because preview lookup and the `preview=` prompt argument are
absent.

- [ ] **Step 3: Add disabled-by-default rollout gates**

Extend `SidebarConfig` in `session_bridge/config.py`:

```python
@dataclass(frozen=True)
class SidebarConfig:
    enabled: bool = False
    continuous: bool = False
    backfill_days: int = 30
    continuous_batch_limit: int = 5
    readable_preview_enabled: bool = False
    legacy_hydration_enabled: bool = False
    preview_budget_chars: int = 24_000
```

Parse only those exact keys under `[session_bridge.sidebar]`; validate
`preview_budget_chars` in `1..100_000`.

- [ ] **Step 4: Add one transactional indexed snapshot read**

Add to `SessionBridgeStore`:

```python
def get_sidebar_preview_source(
    self,
    source_session_id: str,
) -> dict[str, Any]:
    """Read session, active messages, and native cursor/hash in one snapshot."""
```

Use the same profile-shadow authority and `_decode_content` rules as
`ContextPackBuilder._read_source_snapshot`. Return only indexed session metadata,
active decoded messages, and the exact authoritative cursor/hash. Raise fixed
identity errors for absent, ambiguous, or mismatched sources.

- [ ] **Step 5: Make registration prompt rendering version-aware**

Change `build_registration_prompt` to:

```python
def build_registration_prompt(
    candidate: SidebarCandidate,
    marker: str,
    *,
    preview: SessionPreview | None = None,
) -> str:
```

`preview=None` must produce the exact legacy 13-line prompt. A preview must produce
the approved readable order and a bridge block containing:

```text
Preview version: 1
Preview source cursor: "<cursor>"
Preview source hash: "<hash>"
Preview digest: "<64 lowercase hex>"
```

Do not add fields to `HERMES_SESSION_BRIDGE_V1`. Validate the digest separately and
keep exact legacy marker parsing intact.

Add `is_registration_prompt(value)` that recognizes either the exact legacy block
or a structurally valid readable registration with one exact signed marker and
valid preview metadata. Replace callers of `_is_exact_registration_block` where
both versions must be excluded.

- [ ] **Step 6: Integrate preview generation into one leased broker job**

When `readable_preview_enabled` is true, `_build_sidebar_broker_job` must:

1. read one indexed source snapshot;
2. verify its source ID and provider match the leased candidate;
3. build a deterministic preview at the configured budget;
4. call
   `build_registration_prompt(candidate, marker, preview=preview)`.

Pass the configured preview budget into the helper and call the pure
`build_session_preview` function instead of reading global config. Any failure
settles that lease with `source_identity_mismatch`; no native task is created.

- [ ] **Step 7: Run prompt, store, coordinator, and MCP tests**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_preview.py tests\session_bridge\test_sidebar.py tests\session_bridge\test_store.py tests\session_bridge\test_coordinator.py tests\session_bridge\test_mcp_server.py -k "preview or registration or sidebar_pending" -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit readable registration delivery**

```powershell
git add session_bridge\config.py session_bridge\store.py session_bridge\coordinator.py session_bridge\sidebar.py session_bridge\mcp_server.py tests\session_bridge\test_preview.py tests\session_bridge\test_sidebar.py tests\session_bridge\test_store.py tests\session_bridge\test_coordinator.py tests\session_bridge\test_mcp_server.py
git commit -m "feat(session-bridge): deliver readable registration prompts"
```

### Task 3: Add the durable legacy hydration state machine

**Files:**

- Modify: `hermes_state.py`
- Modify: `session_bridge/models.py`
- Modify: `session_bridge/store.py`
- Test: `tests/session_bridge/test_store.py`
- Test: `tests/test_hermes_state.py`

- [ ] **Step 1: Write failing schema and transition tests**

Add tests that open a fresh `SessionDB` and assert:

```python
columns = {
    row["name"]
    for row in db._conn.execute(
        "PRAGMA table_info(session_sidebar_hydration_jobs)"
    )
}
assert {
    "id", "source_session_id", "bridge_id", "codex_thread_id",
    "source_cursor", "source_hash", "preview_version", "preview_digest",
    "hydration_marker", "state", "attempts", "next_attempt_at",
    "lease_digest", "lease_expires_at", "send_reserved_at",
    "sent_at", "verified_at", "completion_digest", "error_code",
    "created_at", "updated_at",
}.issubset(columns)
```

Store tests must prove:

- seeding is idempotent by `bridge_id` and exact Codex task ID;
- only `sidebar_visible` jobs with an authenticated legacy candidate can be seeded;
- claim is compare-and-swap safe and permits only one active hydration lease;
- reserve is idempotent for the same lease and freezes cursor/hash/digest;
- commit requires the same task ID and hydration marker;
- ambiguous send failure preserves reservation and enters reconciliation-only retry;
- a reserved row can never return to a state that permits a fresh send;
- no hydration method inserts or modifies `session_sidebar_jobs`.

- [ ] **Step 2: Run schema/store tests and verify failure**

Run:

```powershell
uv run --no-sync pytest tests\test_hermes_state.py tests\session_bridge\test_store.py -k "sidebar_hydration" -q
```

Expected: failures because the table, enum, and store methods are absent.

- [ ] **Step 3: Add hydration state and SQL schema**

Add to `session_bridge/models.py`:

```python
class SidebarHydrationState(StrEnum):
    PENDING = "hydration_pending"
    LEASED = "hydration_leased"
    RETRY = "hydration_retry"
    VISIBLE = "hydration_visible"
    FAILED = "hydration_failed"
```

Add `session_sidebar_hydration_jobs` to `hermes_state.py` with:

- unique `source_session_id`, `bridge_id`, and `codex_thread_id`;
- state CHECK over the five enum values;
- 64-lowercase-hex CHECK for `preview_digest`;
- lease/completion consistency CHECKs;
- indexes on `(state, next_attempt_at)`, non-null lease digest, and non-null
  completion digest.

Use the repository's additive `CREATE TABLE IF NOT EXISTS` migration style; do not
rewrite existing tables.

- [ ] **Step 4: Implement exact store transitions**

Add these exact methods to `SessionBridgeStore`:

- `seed_sidebar_hydration_job(source_session_id: str, bridge_id: str, codex_thread_id: str, source_cursor: str, source_hash: str, preview_version: int, preview_digest: str, hydration_marker: str, now: float) -> dict[str, Any]`
- `claim_sidebar_hydration_jobs(*, now: float, limit: int = 1) -> list[dict[str, Any]]`
- `reserve_sidebar_hydration_send(*, lease_token: str, now: float) -> dict[str, Any]`
- `commit_sidebar_hydration_job(*, lease_token: str, codex_thread_id: str, hydration_marker: str, now: float) -> dict[str, Any]`
- `fail_sidebar_hydration_job(*, lease_token: str, error_code: str, codex_thread_id: str, now: float) -> dict[str, Any]`
- `sidebar_hydration_status(self, now: float) -> dict[str, Any]`

Use a separate HMAC lease namespace from sidebar creation. Fixed errors are:

```python
HYDRATION_RETRYABLE_ERRORS = frozenset({
    "codex_tool_unavailable",
    "native_task_not_indexed",
    "hydration_send_ambiguous",
    "bridge_temporarily_unavailable",
    "broker_time_budget",
})
HYDRATION_FATAL_ERRORS = frozenset({
    "marker_conflict",
    "source_identity_mismatch",
    "codex_thread_conflict",
    "preview_digest_mismatch",
})
```

`hydration_send_ambiguous` is reconciliation-only: preserve
`send_reserved_at`, the exact task ID, marker, cursor/hash, and digest. Future claims
return `send_reserved=True` and never authorize another send.

- [ ] **Step 5: Run schema and store tests**

Run:

```powershell
uv run --no-sync pytest tests\test_hermes_state.py tests\session_bridge\test_store.py -k "sidebar_hydration" -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit the hydration state machine**

```powershell
git add hermes_state.py session_bridge\models.py session_bridge\store.py tests\test_hermes_state.py tests\session_bridge\test_store.py
git commit -m "feat(session-bridge): persist exact-task hydration jobs"
```

### Task 4: Expose authenticated hydration broker tools

**Files:**

- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/mcp_server.py`
- Modify: `session_bridge/broker_client.py`
- Test: `tests/session_bridge/test_sidebar.py`
- Test: `tests/session_bridge/test_coordinator.py`
- Test: `tests/session_bridge/test_mcp_server.py`
- Test: `tests/session_bridge/test_broker_client.py`

- [ ] **Step 1: Write failing marker and MCP contract tests**

Define tests for a separate signed marker:

```text
HERMES_SESSION_HYDRATION_V1:<payload>.<signature>
```

The canonical payload contains exactly:

```python
{
    "bridge_id",
    "codex_thread_id",
    "preview_digest",
    "preview_version",
    "source_cursor",
    "source_hash",
    "source_session_id",
}
```

MCP tests must assert exact tool names and schemas:

```python
session_sidebar_hydration_pending(limit=1)
session_sidebar_hydration_reserve(lease_token)
session_sidebar_hydration_commit(
    lease_token, codex_thread_id, hydration_marker
)
session_sidebar_hydration_fail(
    lease_token, error_code, codex_thread_id
)
```

Pending returns one bounded job with lease token, exact task ID, readable hydration
message, exact hydration marker, cwd/git-root grouping metadata, and
`send_reserved`. It never returns raw source rows or provider-native paths.

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_sidebar.py tests\session_bridge\test_mcp_server.py tests\session_bridge\test_broker_client.py -k "hydration" -q
```

Expected: failures because the marker and tools are absent.

- [ ] **Step 3: Implement signed hydration markers**

In `session_bridge/sidebar.py`, add
`encode_hydration_marker(payload: HydrationMarkerPayload, secret: bytes) -> str`
and
`decode_hydration_marker(marker: str, secret: bytes) -> HydrationMarkerPayload`.
They use the existing marker key and HMAC algorithm but a distinct prefix and exact
field set.

Reject extra/missing fields, non-canonical base64url, uppercase digests, task/source
identity mismatch, or invalid signatures.

- [ ] **Step 4: Add coordinator claim/build methods**

Add:

```python
async def claim_sidebar_hydration_for_delivery(
    self,
    *,
    limit: int = 1,
) -> Sequence[SidebarHydrationClaim]:
```

For an unreserved claim, rebuild the preview and verify cursor/hash/digest before
return. For a reserved claim, load the frozen preview identity and return
`send_reserved=True`; never rebuild into a send-authorized state.

The readable hydration message must include the preview first, then:

```text
This is an authenticated in-place Session Bridge hydration.
Call session_continue(session_id="<source>", target_provider="codex") before project work.
Hydration marker: HERMES_SESSION_HYDRATION_V1:<signed-payload>
After the continuation call, reply only: HYDRATED
```

- [ ] **Step 5: Register MCP tools and loopback parity**

Add the four tools to `EXPECTED_TOOLS`, use exact request validation, and map all
internal exceptions to fixed public codes. Extend `broker_client.py` subcommands:

```text
hydration-pending
hydration-reserve
hydration-commit
hydration-fail
```

Require equals-sign arguments for opaque values, matching the existing sidebar
client.

- [ ] **Step 6: Run hydration MCP/coordinator/client tests**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_sidebar.py tests\session_bridge\test_coordinator.py tests\session_bridge\test_mcp_server.py tests\session_bridge\test_broker_client.py -k "hydration" -q
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit the authenticated broker surface**

```powershell
git add session_bridge\sidebar.py session_bridge\coordinator.py session_bridge\mcp_server.py session_bridge\broker_client.py tests\session_bridge\test_sidebar.py tests\session_bridge\test_coordinator.py tests\session_bridge\test_mcp_server.py tests\session_bridge\test_broker_client.py
git commit -m "feat(session-bridge): expose authenticated hydration leases"
```

### Task 5: Update the Codex broker skill for exact-task hydration

**Files:**

- Modify: `session_bridge/assets/session-sidebar-sync/SKILL.md`
- Test: `tests/session_bridge/test_sidebar_skill.py`
- Test: `tests/session_bridge/test_end_to_end.py`

- [ ] **Step 1: Write failing skill-contract tests**

Extend the parser in `tests/session_bridge/test_end_to_end.py` and add assertions
that one broker wake:

- calls `session_status` exactly once;
- prefers one actionable hydration retry/pending job before a new registration;
- claims at most one total job across both queues;
- reads the exact linked task before any send;
- reconciles an existing exact hydration marker without sending;
- reserves immediately before `send_message_to_thread`;
- sends only to the exact authenticated task ID;
- treats every raised/missing/uncertain send result as
  `hydration_send_ambiguous`;
- never calls `create_thread` during hydration;
- commits only after the exact marker and a completed turn are readable;
- calls hydration fail at most once for an unfinished lease.

Add a static assertion:

```python
assert "create_thread" not in hydration_procedure_text
assert "send_message_to_thread" in hydration_procedure_text
```

- [ ] **Step 2: Run skill/end-to-end tests and verify failure**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_sidebar_skill.py tests\session_bridge\test_end_to_end.py -k "hydration or contract" -q
```

Expected: failures because the installed skill has no hydration procedure.

- [ ] **Step 3: Add a deterministic queue choice**

Update the skill quick reference and procedure:

1. call `session_status` once;
2. if hydration pending/retry is nonzero, preflight projects and call
   `session_sidebar_hydration_pending(limit=1)`;
3. otherwise follow the existing registration path;
4. process exactly one total lease per wake.

Do not call both pending tools in one wake.

- [ ] **Step 4: Add exact-task hydration reconciliation**

The skill must:

1. choose the existing saved project using exact cwd/git-root/Session Inbox rules;
2. call `read_thread` for the returned exact task ID with
   `turnLimit=10, includeOutputs=false`;
3. authenticate local host, project/cwd, legacy signed source marker, and task ID;
4. search only returned turns for the exact hydration marker;
5. commit immediately if an authenticated completed hydration turn is already
   present;
6. if `send_reserved=true` and the marker is absent, fail once with
   `hydration_send_ambiguous`;
7. otherwise reserve, invoke
   `send_message_to_thread({"threadId":"<exact id>","message":"<hydration_message verbatim>"})`,
   then poll only that exact task;
8. commit after exact marker plus completed turn;
9. never create, rename, archive, or replace a task in hydration mode.

- [ ] **Step 5: Add deterministic send-failure mapping**

Document and test:

- tool unavailable before send -> `codex_tool_unavailable`;
- reserve failure -> `bridge_temporarily_unavailable`;
- any uncertainty after send invocation -> `hydration_send_ambiguous`;
- task/marker mismatch -> `marker_conflict`;
- host/project/cwd conflict -> `codex_thread_conflict`;
- unreadable/not-quiescent task -> `native_task_not_indexed`;
- exhausted safe time -> `broker_time_budget`.

No exception text or preview content appears in status/failure output.

- [ ] **Step 6: Run skill and end-to-end tests**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_sidebar_skill.py tests\session_bridge\test_end_to_end.py -k "sidebar or hydration" -q
```

Expected: all selected tests pass, including ambiguous-send replay with exactly one
native message.

- [ ] **Step 7: Commit the skill contract**

```powershell
git add session_bridge\assets\session-sidebar-sync\SKILL.md tests\session_bridge\test_sidebar_skill.py tests\session_bridge\test_end_to_end.py
git commit -m "feat(session-bridge): hydrate exact legacy sidebar tasks"
```

### Task 6: Add guarded CLI rollout and sanitized status

**Files:**

- Modify: `session_bridge/cli.py`
- Modify: `session_bridge/config.py`
- Modify: `session_bridge/mcp_server.py`
- Test: `tests/session_bridge/test_cli.py`
- Test: `tests/session_bridge/test_mcp_server.py`

- [ ] **Step 1: Write failing CLI and status tests**

Cover:

```text
session-bridge sidebar-readable-preview --enable|--disable
session-bridge sidebar-hydration --enable|--disable
session-bridge sidebar-hydration-seed --source-session-id <id> --codex-thread-id <id> --confirm HYDRATE_EXACT_EXISTING_TASK
session-bridge sidebar-hydration-status
```

Tests must prove:

- the exact source/task IDs must match an existing visible link;
- the confirmation token is mandatory;
- bulk seeding is unavailable in the canary command;
- status contains counts, oldest age, and fixed error codes only;
- lease tokens, markers, preview text, source messages, and native paths are absent;
- enabling hydration does not create an automation.

- [ ] **Step 2: Run CLI/status tests and verify failure**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_cli.py tests\session_bridge\test_mcp_server.py -k "readable_preview or hydration" -q
```

Expected: parser and backend failures because the commands are absent.

- [ ] **Step 3: Implement exact canary seeding**

The seed backend must:

1. require both exact IDs and `HYDRATE_EXACT_EXISTING_TASK`;
2. verify the sidebar job is `sidebar_visible`;
3. verify its `codex_thread_id` equals the supplied task;
4. verify the mirrors link uses the same bridge/source/task;
5. build the current preview and signed hydration marker;
6. insert one idempotent pending hydration row;
7. return only job ID, source ID, task ID, state, preview version, and digest.

Do not add an `--all` option in this change.

- [ ] **Step 4: Implement feature-gate persistence and status**

Persist only the two new booleans in the existing TOML update mechanism. Expose
hydration counts through `session_status.sidebar.hydration`:

```python
{
    "enabled": bool,
    "counts": {
        "hydration_pending": int,
        "hydration_leased": int,
        "hydration_retry": int,
        "hydration_visible": int,
        "hydration_failed": int,
    },
    "oldest_pending_age_seconds": float | None,
    "recent_error_codes": list[str],
}
```

- [ ] **Step 5: Run CLI and status tests**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge\test_cli.py tests\session_bridge\test_mcp_server.py -k "readable_preview or hydration" -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit rollout controls**

```powershell
git add session_bridge\cli.py session_bridge\config.py session_bridge\mcp_server.py tests\session_bridge\test_cli.py tests\session_bridge\test_mcp_server.py
git commit -m "feat(session-bridge): guard readable hydration rollout"
```

### Task 7: Prove full regression behavior

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Modify: `tests/session_bridge/test_coordinator_safety.py`
- Modify: `tests/session_bridge/test_coordinator_hardening.py`
- Modify: `docs/superpowers/specs/2026-07-26-session-sidebar-readable-hydration-design.md`

- [ ] **Step 1: Add the new-import end-to-end fixture**

Create a fixture with seven user/assistant messages, one tool result, one system
message, a fake secret, and git metadata. Deliver one registration and assert:

- the prompt begins with the readable heading;
- exactly the last five conversational messages are present;
- tool/system content and the secret are absent;
- the signed source marker remains exact;
- the native agent replies `REGISTERED`;
- no `session_continue` call occurs during registration.

- [ ] **Step 2: Add the reported legacy-task recovery fixture**

Seed:

```text
source: claude:2a786924-8093-4a9f-a371-6e27ca66be32
task:   codex:019f8927-8012-77d0-beb0-4cd5f8cc21f9
messages: 578
```

The fixture may synthesize message bodies but must preserve the exact counts and
identity shape. Assert one hydration run appends one readable preview to that same
task, calls `session_continue`, commits hydration, and never calls `create_thread`,
rename, archive, or replacement creation.

- [ ] **Step 3: Add ambiguous-send reconciliation**

Make the native send adapter append the message and then drop its response. The
first worker cycle must fail with `hydration_send_ambiguous`. The next cycle must
find the exact hydration marker, commit the original task, and leave exactly one
hydration message.

- [ ] **Step 4: Add continuation non-regression**

After both new registration and legacy hydration, invoke continuation and assert:

- one immutable context pack is frozen;
- exact cwd/worktree preflight still runs;
- link relation becomes `continues`;
- preview generation alone never sets `hydrated_at`;
- source cursor/hash drift is handled by the existing continuation rules.

- [ ] **Step 5: Run the entire Session Bridge suite**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge -q
```

Expected: all Session Bridge tests pass with no unexpected skip or xfail increase.

- [ ] **Step 6: Run lint and diff verification**

Run:

```powershell
uv run --no-sync ruff check session_bridge hermes_state.py tests\session_bridge tests\test_hermes_state.py
git diff --check
```

Expected: both commands exit 0.

- [ ] **Step 7: Reconcile final names back into the spec**

Update only concrete API names that changed during implementation review. Do not
weaken acceptance criteria, ambiguous-send quarantine, exact-task preservation, or
the five-message contract.

- [ ] **Step 8: Commit end-to-end proof**

```powershell
git add tests\session_bridge\test_end_to_end.py tests\session_bridge\test_coordinator_safety.py tests\session_bridge\test_coordinator_hardening.py docs\superpowers\specs\2026-07-26-session-sidebar-readable-hydration-design.md
git commit -m "test(session-bridge): prove readable hydration recovery"
```

### Task 8: Install and perform the single-task canary

**Files:**

- Modify: installed asset
  `C:\Users\diego\.codex\skills\session-sidebar-sync\SKILL.md`
- Read: `C:\Users\diego\.hermes\config.toml`
- Read: `C:\Users\diego\.hermes\state.db`

- [ ] **Step 1: Verify the branch before touching live assets**

Run:

```powershell
uv run --no-sync pytest tests\session_bridge -q
uv run --no-sync ruff check session_bridge hermes_state.py tests\session_bridge tests\test_hermes_state.py
git status --short
```

Expected: tests and Ruff pass; the worktree is clean.

- [ ] **Step 2: Install the reviewed skill through the existing installer**

Use the repository's `session-bridge install` command tested by
`tests/session_bridge/test_cli.py`. Verify the installed skill hash matches the
branch asset. Do not edit the installed file directly.

- [ ] **Step 3: Enable readable prompts only**

Run the guarded CLI to enable `readable_preview_enabled`. Keep
`legacy_hydration_enabled` false. Do not create any heartbeat, cron, reminder, or
other automation.

- [ ] **Step 4: Deliver one disposable new-session canary manually**

Invoke one broker cycle manually and verify:

- readable brief and five messages are visible;
- task/source marker identities match;
- registration is quiescent;
- no secret/tool/system content appears;
- continuation succeeds after one substantive message.

Disable the readable-preview gate immediately if any check fails.

- [ ] **Step 5: Seed only the reported exact legacy task**

Run:

```text
session-bridge sidebar-hydration-seed
  --source-session-id claude:2a786924-8093-4a9f-a371-6e27ca66be32
  --codex-thread-id 019f8927-8012-77d0-beb0-4cd5f8cc21f9
  --confirm HYDRATE_EXACT_EXISTING_TASK
```

Enable legacy hydration and invoke one hydration broker cycle manually.

- [ ] **Step 6: Verify the exact task in place**

Read the exact Codex task and verify:

- one readable preview and one hydration marker exist;
- the brief and last five messages are visible;
- `session_continue` completed;
- the task ID/title/project are unchanged;
- no second task exists for the source or marker;
- the hydration row is visible/completed.

- [ ] **Step 7: Keep background scheduling disabled**

Do not recreate `session-sidebar-sync-worker`. Report canary evidence to the user
and request explicit approval before designing any replacement background
scheduler or bulk legacy hydration rollout.

- [ ] **Step 8: Record live evidence**

Write one MemPalace drawer in wing `hermes`, room `session-bridge`, and add a
timeline entry to `systems/cross-harness-session-bridge`. Include commit IDs,
exact canary source/task IDs, test counts, feature-gate state, and the explicit fact
that no recurring automation was recreated.
