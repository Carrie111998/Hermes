# Codex Native Sidebar Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Make every recent, meaningful native Claude Code or Hermes session appear as a native task in the Codex Desktop left sidebar within one minute, while preserving exact source cwd/worktree continuation and a unified Hermes catalog.

**Architecture:** Session Bridge remains the source of truth. It classifies eligible source sessions and owns a new durable `session_sidebar_jobs` queue with atomic leases and signed lineage. A personal Codex skill, invoked by a Codex-owned one-minute scheduled task, leases work over MCP and uses native Codex task tools to create, title, verify, and commit sidebar tasks. Existing external mirror jobs remain unchanged, and rollout stays disabled until manual and scheduled canaries prove native sidebar visibility, exact-cwd access, and duplicate-safe reconciliation.

**Tech Stack:** Python 3.11+, SQLite, FastMCP/Starlette, pytest through `scripts/run_tests.sh`, React 19/TypeScript/Vitest, Codex personal skills, Codex Desktop native task and automation tools.

---

## Scope and invariants

- Implement the approved design in `docs/superpowers/specs/2026-07-15-codex-native-sidebar-broker-design.md`.
- Do not modify Codex SQLite, global UI state, application packages, or provider-native transcript stores.
- Do not route sidebar delivery through `codex app-server thread/start`.
- Do not reuse or change `session_mirror_jobs`; add `session_sidebar_jobs`.
- Do not add non-secret environment variables. Sidebar settings live under `session_bridge.sidebar` in `~/.hermes/config.yaml`; secrets continue using existing restricted files.
- Resolve Hermes paths with `get_hermes_home()`. Resolve a personal Codex home from the already-supported `CODEX_HOME`, falling back to `Path.home() / ".codex"`; do not introduce a new environment name.
- Keep automatic sidebar enqueue and the one-minute scheduled task disabled until Tasks 12 and 13 pass.
- Run Python tests only through `scripts/run_tests.sh`.
- Commit after every green task. Never include unrelated dirty-worktree changes.

## Fixed contract

```python
SIDEBAR_IDEMPOTENCY_VERSION = 1
SIDEBAR_CONTINUOUS_LIMIT = 5
SIDEBAR_BACKFILL_LIMIT = 10
SIDEBAR_LEASE_SECONDS = 300
SIDEBAR_RETRY_DELAYS_SECONDS = (60, 120, 240, 480, 900)
SIDEBAR_MAX_ATTEMPTS = 5
```

The MCP surface is exactly:

```text
session_sidebar_pending(limit=5)
session_sidebar_commit(lease_token, codex_thread_id)
session_sidebar_fail(lease_token, error_code)
```

The job key is exactly:

```text
codex-sidebar:<canonical-source-session-id>:v1
```

The public states are exactly:

```text
sidebar_pending
sidebar_leased
sidebar_visible
sidebar_retry
sidebar_failed
```

## Task 1: Add the sidebar job schema and domain types

**Files:**

- Modify: `hermes_state.py:142`
- Modify: `hermes_state.py:839-930`
- Modify: `session_bridge/models.py:26-42`
- Modify: `tests/hermes_state/test_session_bridge_schema.py`
- Modify: `tests/session_bridge/test_models.py`

### Step 1: Write failing schema and enum tests

Extend `EXPECTED_BRIDGE_TABLES`, `EXPECTED_BRIDGE_INDEXES`, and `EXPECTED_BRIDGE_FOREIGN_KEYS` with `session_sidebar_jobs`. Assert that a fresh database is schema v21, that a v20 database upgrades without changing existing rows, and that the state check rejects unknown values.

Add this model test:

```python
def test_sidebar_job_states_are_the_public_contract() -> None:
    assert [state.value for state in SidebarJobState] == [
        "sidebar_pending",
        "sidebar_leased",
        "sidebar_visible",
        "sidebar_retry",
        "sidebar_failed",
    ]
```

Run:

```bash
bash scripts/run_tests.sh tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_models.py -q
```

Expected: FAIL because schema v21, `session_sidebar_jobs`, and `SidebarJobState` do not exist.

### Step 2: Add the enum and additive DDL

Add to `session_bridge/models.py`:

```python
class SidebarJobState(StrEnum):
    PENDING = "sidebar_pending"
    LEASED = "sidebar_leased"
    VISIBLE = "sidebar_visible"
    RETRY = "sidebar_retry"
    FAILED = "sidebar_failed"
```

Bump `SCHEMA_VERSION = 21` and add this table to `BRIDGE_SCHEMA_SQL`:

```sql
CREATE TABLE IF NOT EXISTS session_sidebar_jobs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    source_session_id TEXT NOT NULL REFERENCES sessions(id),
    bridge_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (
        state IN (
            'sidebar_pending', 'sidebar_leased', 'sidebar_visible',
            'sidebar_retry', 'sidebar_failed'
        )
    ),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at REAL NOT NULL,
    lease_digest TEXT,
    lease_expires_at REAL,
    completion_digest TEXT,
    codex_thread_id TEXT UNIQUE,
    error_code TEXT,
    eligible_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    visible_at REAL,
    CHECK (
        (state = 'sidebar_leased' AND lease_digest IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state != 'sidebar_leased' AND lease_digest IS NULL AND lease_expires_at IS NULL)
    ),
    CHECK (
        state != 'sidebar_visible'
        OR (
            codex_thread_id IS NOT NULL
            AND visible_at IS NOT NULL
            AND completion_digest IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_session_sidebar_jobs_state_next_attempt_at
    ON session_sidebar_jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_session_sidebar_jobs_source_session_id
    ON session_sidebar_jobs(source_session_id);
```

Use the existing additive bridge-DDL transaction. Do not write a destructive migration.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_models.py -q
```

Expected: PASS.

Commit:

```bash
git add hermes_state.py session_bridge/models.py tests/hermes_state/test_session_bridge_schema.py tests/session_bridge/test_models.py
git commit -m "feat(session-bridge): add sidebar delivery schema"
```

## Task 2: Implement deterministic meaningful-session classification and payload construction

**Files:**

- Create: `session_bridge/sidebar.py`
- Create: `tests/session_bridge/test_sidebar.py`
- Modify: `session_bridge/models.py`

### Step 1: Write the classifier matrix first

Cover both Claude and Hermes source rows. Tests must prove:

- `fix it` and `build X` are meaningful.
- sole `ok`, `yes`, whitespace, `READY`, `/resume`, `/clear`, `/help`, and `/quit` are not meaningful.
- tool/system/developer messages do not count.
- signed bridge registration content does not count.
- automation-only and subagent-only sessions do not count.
- three Unicode letters/digits count without stripping accents.
- exactly 30 days is eligible and older than 30 days is ineligible during backfill.
- bridge placeholders and continuation targets are ineligible.
- titles are `[Claude] <redacted title>` or `[Hermes] <redacted title>`, bounded to 120 characters.

Use table-driven tests with a fixed `now=1_800_000_000.0`; never call the wall clock in classification tests.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar.py -q
```

Expected: FAIL because `session_bridge.sidebar` does not exist.

### Step 2: Implement pure classifier and registration value objects

The module must expose these stable entry points:

```python
ACK_OR_CONTROL_ONLY = frozenset({
    "ok", "okay", "yes", "y", "ready", "resume", "/resume",
    "clear", "/clear", "help", "/help", "quit", "/quit",
})

@dataclass(frozen=True)
class SidebarCandidate:
    source_session_id: str
    provider: Provider
    bridge_id: str
    title: str
    cwd: str
    git_root: str | None
    git_branch: str | None
    git_head: str | None
    worktree_id: str | None
    eligible_at: float

def normalize_meaningful_user_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).strip()
    return normalized or None

def is_meaningful_user_text(value: object) -> bool:
    normalized = normalize_meaningful_user_text(value)
    if normalized is None or normalized.casefold() in ACK_OR_CONTROL_ONLY:
        return False
    return sum(character.isalnum() for character in normalized) >= 3

def sidebar_idempotency_key(source_session_id: str) -> str:
    return f"codex-sidebar:{source_session_id}:v1"

def sidebar_bridge_id(source_session_id: str) -> str:
    digest = hashlib.sha256(sidebar_idempotency_key(source_session_id).encode()).hexdigest()
    return f"sidebar:{digest}"
```

Also implement `sidebar_title(provider, title, first_request)` and
`build_registration_prompt(candidate, marker)` with these exact signatures:
`sidebar_title(provider: Provider, title: str | None, first_request: str) ->
str` and `build_registration_prompt(candidate: SidebarCandidate, marker: str)
-> str`.
The title uses the deterministic provider prefix, existing title redaction, and
a 120-character bound. The registration prompt emits the fixed registration
statement, marker, canonical source ID, exact worktree fields, and continuation
instruction in a stable field order.

The meaningful predicate is:

```python
normalized = unicodedata.normalize("NFKC", text).strip()
if normalized.casefold() in ACK_OR_CONTROL_ONLY:
    return False
return sum(character.isalnum() for character in normalized) >= 3
```

Apply the approved structural exclusions before this predicate. Redact titles and prompt metadata with the existing deterministic redaction helpers; never include transcript bodies in registration prompts.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar.py tests/session_bridge/test_models.py -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/sidebar.py session_bridge/models.py tests/session_bridge/test_sidebar.py
git commit -m "feat(session-bridge): classify sidebar-eligible sessions"
```

## Task 3: Add durable enqueue, lease, commit, fail, and reconciliation state

**Files:**

- Modify: `session_bridge/store.py:360-700`
- Modify: `session_bridge/store.py:699-1265`
- Modify: `tests/session_bridge/test_store.py`

### Step 1: Write state-machine tests

Add tests for all of these invariants:

- enqueue is idempotent for `codex-sidebar:<source>:v1`;
- one source creates one bridge ID and job;
- leasing is atomic, ordered by `eligible_at, id`, and bounded to `1..10`;
- lease tokens are returned once but only SHA-256 digests are persisted;
- lease duration is exactly 300 seconds;
- expired leases become `sidebar_retry` and can be leased again;
- commit requires the exact unexpired token;
- exact repeated commit is idempotent;
- a different task ID after success fails closed;
- failure codes must be from a fixed allowlist;
- retry delays are 60, 120, 240, 480, and 900 seconds plus injected bounded jitter;
- attempt five produces `sidebar_failed`;
- a leased job can be safely released when a broker time budget ends;
- provider A rows remain claimable when provider B has a malformed row.

Inject a deterministic clock, token factory, and jitter function. Assert no plaintext lease token is stored.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py -k sidebar -q
```

Expected: FAIL because the sidebar store methods do not exist.

### Step 2: Implement the store API in one transaction per transition

Add these exact methods to `SessionBridgeStore`: `enqueue_sidebar_job(candidate:
SidebarCandidate) -> dict[str, Any]`, `claim_sidebar_jobs(*, now: float, limit:
int, lease_seconds: int = 300) -> list[dict[str, Any]]`,
`commit_sidebar_job(*, lease_token: str, codex_thread_id: str, now: float) ->
dict[str, Any]`, `fail_sidebar_job(*, lease_token: str, error_code: str, now:
float) -> dict[str, Any]`, `release_sidebar_job(*, lease_token: str, now:
float) -> dict[str, Any]`, `sidebar_job_counts() -> dict[str, int]`, and
`get_sidebar_job_for_source(source_session_id: str) -> dict[str, Any] | None`.

Claim with `BEGIN IMMEDIATE`, first recover expired leases, then update only selected due rows. Generate `secrets.token_urlsafe(32)`, store `sha256(token).hexdigest()`, and return the plaintext only in that call's response. All token comparisons use `hmac.compare_digest`.

Count failed attempts when `fail_sidebar_job` processes a retryable creation or
delivery error, not when a lease is claimed. Add nonnegative jitter uniformly
bounded by `min(30 seconds, 10% of the base delay)`. A fatal code transitions
immediately to `sidebar_failed`. The special fixed code `broker_time_budget`
releases the lease back to `sidebar_pending` without consuming an attempt or
adding delay.

On success, atomically move the active digest from `lease_digest` to
`completion_digest` before clearing the lease. This lets an exact repeated
commit authenticate and return the original visible row, while a different
thread ID or token still fails closed.

Use fixed error codes only:

```python
SIDEBAR_RETRYABLE_ERRORS = frozenset({
    "codex_tool_unavailable",
    "desktop_offline",
    "bridge_temporarily_unavailable",
    "sqlite_busy",
    "rename_failed",
    "project_lookup_failed",
    "native_task_not_indexed",
    "broker_time_budget",
})
SIDEBAR_FATAL_ERRORS = frozenset({
    "marker_conflict",
    "source_identity_mismatch",
    "codex_thread_conflict",
    "provider_mismatch",
    "source_cwd_missing",
    "permission_preflight_failed",
    "retry_budget_exhausted",
})
```

Do not persist exception text in `error_code` or a new detail field.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py -k sidebar -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): persist sidebar delivery leases"
```

## Task 4: Register eligible Claude and Hermes sessions without creating native tasks

**Files:**

- Modify: `session_bridge/store.py:360-579`
- Modify: `session_bridge/coordinator.py:98-220`
- Modify: `session_bridge/coordinator.py:1140-1225`
- Modify: `session_bridge/coordinator.py:890-950`
- Modify: `session_bridge/config.py`
- Modify: `hermes_cli/config.py:906`
- Modify: `hermes_cli/config.py:5129`
- Modify: `tests/session_bridge/test_coordinator.py`
- Modify: `tests/session_bridge/test_config_safety.py`

### Step 1: Add failing eligibility-enqueue and config tests

Tests must prove that a scan:

- queues a native meaningful Claude session;
- queues a native meaningful Hermes session;
- does not queue Codex sources, bridge-origin sessions, empty sessions, acknowledgements, automation runs, or subagents;
- does not call either target adapter;
- does not enqueue the same canonical source twice;
- keeps Claude and Hermes registration isolated;
- respects the 30-day backfill cutoff;
- leaves continuous enqueue off by default;
- reads sidebar configuration from `session_bridge.sidebar` in `config.yaml`;
- rejects unknown sidebar keys and does not add sidebar environment names.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py tests/session_bridge/test_config_safety.py -k sidebar -q
```

Expected: FAIL because sidebar registration and configuration are absent.

### Step 2: Add config.yaml schema and immutable defaults

Add `session_bridge` to `_KNOWN_ROOT_KEYS` and this subtree to `DEFAULT_CONFIG`:

```python
"session_bridge": {
    "sidebar": {
        "enabled": False,
        "continuous": False,
        "backfill_days": 30,
        "continuous_batch_limit": 5,
        "manual_batch_limit": 10,
        "lease_seconds": 300,
        "max_attempts": 5,
        "heartbeat_grace_seconds": 120,
    },
},
```

Add `SidebarConfig` to `session_bridge/config.py` and load that subtree through
`hermes_cli.config.load_config()`. Validate exact keys, strict booleans, limits
`1..10`, `backfill_days >= 0`, `lease_seconds == 300`, `max_attempts == 5`, and
`heartbeat_grace_seconds >= 0`. Do not add `_ENV_NAMES` entries.

### Step 3: Add one provider-neutral candidate query and coordinator registration pass

Add `SessionBridgeStore.list_sidebar_candidates(after, limit, cursor=None)` using batched SQL to fetch source metadata plus user messages. Keep message semantics in `sidebar.py`, not SQL. Include native Claude rows from `external_sessions` and native Hermes rows from `sessions` without `external_sessions`; exclude Codex and bridge lineage before classification.

Add `SessionBridgeCoordinator.register_sidebar_jobs_once(now=None, limit=100)` that classifies and enqueues only. Invoke it after successful provider indexing only when `sidebar.enabled` is true. When `continuous` is false, permit explicit backfill registration but not background registration.

Health adds sanitized registration counts but never lease data.

### Step 4: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py tests/session_bridge/test_config_safety.py -k sidebar -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/store.py session_bridge/coordinator.py session_bridge/config.py hermes_cli/config.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_config_safety.py
git commit -m "feat(session-bridge): queue eligible sidebar sessions"
```

## Task 5: Verify signed native Codex registration and reconcile ambiguous creation

**Files:**

- Modify: `session_bridge/sidebar.py`
- Modify: `session_bridge/coordinator.py`
- Modify: `session_bridge/codex_adapter.py`
- Create: `tests/session_bridge/test_sidebar_reconciliation.py`
- Modify: `tests/session_bridge/test_target_adapters.py`

### Step 1: Write failing verification and ambiguity tests

Use a fake read-only Codex inventory. Cover:

- the exact returned thread contains one valid marker bound to source, bridge, target `codex`, and policy generation 1;
- an invalid signature, wrong source, wrong bridge, or duplicate valid marker fails closed;
- a newly created thread not yet indexed returns retryable `native_task_not_indexed`;
- if create succeeded but commit was lost, lookup by authenticated marker recovers the one exact thread;
- zero matches allows retry only after the previous lease expires;
- multiple matches produce fatal `marker_conflict` and never create another task;
- this path never calls `CodexTargetAdapter.create_placeholder` or app-server `thread/start`.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_target_adapters.py -q
```

Expected: FAIL because native registration verification is absent.

### Step 2: Implement a read-only verifier

Add the frozen `VerifiedSidebarThread(thread_id, source_session_id, bridge_id)`
value object and `SidebarThreadVerifier`. Its exact public methods are
`verify_thread(*, thread_id: str, expected: BridgeMarkerPayload) ->
VerifiedSidebarThread` and `find_by_marker(expected: BridgeMarkerPayload) ->
VerifiedSidebarThread | None`.

Reuse `decode_bridge_marker()` and the Codex source adapter's read-only list/read transport. Do not add a creation method to this class. Bound polling to the existing service reconciliation interval and return fixed codes.

Before leasing a retry after an ambiguous result, reconcile by authenticated marker. If one match exists, commit it; if more than one exists, mark fatal; only zero proven matches permits a new create attempt.

When reconciliation finds one task after a prior rename failure, rename that
existing task before commit. Never create a replacement merely to repair its
title.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_target_adapters.py -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/sidebar.py session_bridge/coordinator.py session_bridge/codex_adapter.py tests/session_bridge/test_sidebar_reconciliation.py tests/session_bridge/test_target_adapters.py
git commit -m "feat(session-bridge): verify native sidebar lineage"
```

## Task 6: Expose the three authenticated MCP broker operations

**Files:**

- Modify: `session_bridge/mcp_server.py:26-36`
- Modify: `session_bridge/mcp_server.py:196-365`
- Modify: `tests/session_bridge/test_mcp_server.py:600-760`

### Step 1: Write failing MCP contract tests

Update the exact tool set to eight tools. Assert:

- `pending` clamps `limit` to `1..5` and returns only allowlisted fields;
- pending responses contain a plaintext lease token, minimal registration prompt, title, provider, exact cwd/worktree metadata, and no transcript/native path/secret;
- `commit` verifies the thread through the coordinator before changing state;
- `fail` accepts only fixed error codes;
- token, marker, and lease values are redacted from status and error output;
- bearer authentication and loopback binding still apply;
- repeated exact commit is idempotent.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_mcp_server.py -k "sidebar or tool_registration" -q
```

Expected: FAIL because the three tools are missing.

### Step 2: Register the exact tools

Change the set to:

```python
EXPECTED_TOOLS = {
    "session_search",
    "session_get",
    "session_continue",
    "session_mirror",
    "session_status",
    "session_sidebar_pending",
    "session_sidebar_commit",
    "session_sidebar_fail",
}
```

Implement each handler as a thin `asyncio.to_thread` call into coordinator/store logic. `session_sidebar_pending` must use limit five even if a larger value is supplied. `session_sidebar_commit` accepts only `lease_token` and `codex_thread_id`; server-side verification derives the expected marker from the leased job. `session_sidebar_fail` accepts only `lease_token` and `error_code`.

After verification and before returning commit success, ensure the indexed
Codex placeholder is bound to the source with the existing `session_links`
lineage. This makes the first `session_continue(session_id=<source>,
target_provider="codex")` hydrate the registered native task instead of
creating another Codex placeholder.

Add sidebar counts, oldest pending age, last heartbeat, last visible task ID, recent fixed failure codes, and latency percentiles to sanitized `session_status`.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_mcp_server.py -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/mcp_server.py tests/session_bridge/test_mcp_server.py
git commit -m "feat(session-bridge): expose sidebar broker MCP"
```

## Task 7: Package and install the personal Codex sidebar-sync skill

**Files:**

- Create: `session_bridge/assets/session-sidebar-sync/SKILL.md`
- Create: `session_bridge/assets/session-sidebar-sync/agents/openai.yaml`
- Create: `session_bridge/sidebar_skill.py`
- Modify: `session_bridge/cli.py`
- Modify: `pyproject.toml`
- Create: `tests/session_bridge/test_sidebar_skill.py`
- Modify: `tests/session_bridge/test_cli.py`

### Step 1: Write failing installer and skill-content tests

**Required sub-skills for this task:** read and follow `skill-creator`,
`superpowers:writing-skills`, and `superpowers:test-driven-development` before
creating the skill asset. Read the skill-creator `references/openai_yaml.md`
before generating UI metadata.

Tests must use temporary Hermes, home, and Codex directories. Assert:

- install destination is `<codex-home>/skills/session-sidebar-sync/SKILL.md`;
- installation is atomic and idempotent;
- existing different content is backed up before replacement;
- no real `~/.codex` path is touched;
- packaged wheel data contains the skill asset;
- skill text names only the three sidebar MCP tools plus native Codex project/task tools;
- the skill explicitly forbids app-server fallback, transcript copying, duplicate creation, and creating without a lease;
- empty pending batches exit quietly;
- unfinished leases are failed/released before exit.

Before creating the asset, run at least one broker pressure scenario without
the skill and record the failure in the test fixture: simultaneous pending
jobs, an ambiguous prior create, a near-expiry lease, and pressure to fall back
to app-server creation. The baseline must demonstrate at least one unsafe or
incomplete choice. This is the skill RED phase. If executing inline and no
independent agent is authorized, stop this task and request explicit permission
for the forward-test agent rather than skipping the baseline.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_cli.py -k sidebar_skill -q
```

Expected: FAIL because the asset and installer do not exist.

### Step 2: Write the deterministic skill

Initialize the checked-in source skill with the official generator rather than
hand-scaffolding it:

```powershell
python C:\Users\diego\.codex\skills\.system\skill-creator\scripts\init_skill.py session-sidebar-sync --path session_bridge\assets --interface display_name="Session Sidebar Sync" --interface short_description="Deliver leased Claude and Hermes sessions to the Codex sidebar" --interface default_prompt="Run $session-sidebar-sync once and end quietly when no work is pending."
```

Replace every generated placeholder. Keep only `SKILL.md` and
`agents/openai.yaml`; this skill needs no scripts, references, README, or other
auxiliary files.

The skill procedure must be operational, not descriptive:

```markdown
1. Call `session_sidebar_pending(limit=5)` exactly once.
2. If the returned job list is empty, end without a user-facing message.
3. List native Codex projects once and index them by canonical local path.
4. For each leased job, prefer exact cwd project, then exact git root, then the saved `.hermes` Session Inbox.
5. Before creating, search native Codex tasks for the authenticated marker when `reconcile_required` is true.
6. Create one native local task with the returned registration prompt.
7. Rename it to the returned `[Claude]` or `[Hermes]` title.
8. Call `session_sidebar_commit(lease_token, codex_thread_id)`.
9. On a fixed failure, call `session_sidebar_fail` with the mapped code; never send exception text.
10. Never use app-server creation and never create a second task after an ambiguous outcome.
```

The skill must tell the task agent that sidebar grouping does not change command cwd, and that first substantive continuation must call `session_continue`.

Run the same pressure scenario with the skill loaded. It must lease once,
reconcile before creating, refuse app-server fallback, commit completed items,
and safely fail/release unfinished items. Add a variation with an empty batch
and a project-list failure, then tighten the skill until both pass.

### Step 3: Add atomic installer and CLI command

Add:

```python
def resolve_codex_home(environ: Mapping[str, str] | None = None) -> Path:
    environment = os.environ if environ is None else environ
    return Path(environment.get("CODEX_HOME", Path.home() / ".codex")).expanduser()

def install_sidebar_skill(*, codex_home: Path | None = None) -> Path:
    source = resources.files("session_bridge").joinpath("assets", "session-sidebar-sync")
    destination = (codex_home or resolve_codex_home()) / "skills" / "session-sidebar-sync"
    return atomic_install_skill(source=source, destination=destination)
```

Add `hermes-session-bridge install-sidebar-skill`. Package `session_bridge/assets/**/*` in `pyproject.toml`. Use `apply_patch`-style source edits and an atomic temp-file replace at runtime; never shell-copy user paths.

### Step 4: Run tests and commit

Run:

```bash
python C:/Users/diego/.codex/skills/.system/skill-creator/scripts/quick_validate.py session_bridge/assets/session-sidebar-sync
bash scripts/run_tests.sh tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_cli.py -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/assets/session-sidebar-sync/SKILL.md session_bridge/assets/session-sidebar-sync/agents/openai.yaml session_bridge/sidebar_skill.py session_bridge/cli.py pyproject.toml tests/session_bridge/test_sidebar_skill.py tests/session_bridge/test_cli.py
git commit -m "feat(session-bridge): install Codex sidebar sync skill"
```

## Task 8: Enforce exact cwd/worktree identity during continuation

**Files:**

- Create: `session_bridge/worktree.py`
- Modify: `session_bridge/store.py:579-620`
- Modify: `session_bridge/coordinator.py:700-890`
- Modify: `session_bridge/context_pack.py:520-575`
- Create: `tests/session_bridge/test_worktree.py`
- Modify: `tests/session_bridge/test_coordinator.py`
- Modify: `tests/session_bridge/test_context_pack.py`

### Step 1: Write failing identity and permission tests

Create real temporary Git repositories/worktrees. Prove:

- exact canonical cwd, git root, branch, HEAD, and worktree identity are captured;
- unchanged identity passes hydration;
- missing cwd, symlink retarget, replaced repository, different worktree, and changed recorded identity block silently switching locations;
- branch/HEAD drift is reported accurately without pretending the old branch is current;
- an inbox-grouped task still returns the exact source cwd for every command/file operation;
- the registered native Codex task is reused by `session_continue` and no second placeholder is created;
- permission preflight failure returns `permission_preflight_failed` and a visible blocking warning;
- tests never touch a real repository or `~/.hermes`.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_worktree.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_context_pack.py -k "worktree or exact_cwd or permission_preflight" -q
```

Expected: FAIL because persisted worktree identity and preflight are absent.

### Step 2: Implement capture and validation

Expose the frozen `WorktreeSnapshot(cwd, git_root, branch, head, worktree_id)`
value object and the exact functions `capture_worktree_snapshot(cwd: str) ->
WorktreeSnapshot` and `validate_worktree_snapshot(snapshot: WorktreeSnapshot)
-> tuple[WorktreeSnapshot, tuple[str, ...]]`.

Store the immutable snapshot in the sidebar job or a versioned `session_bridge_state` record keyed by source ID. Before `session_continue` returns the context pack, validate it and attach exact-cwd operational instructions. Missing/replaced identity raises a fixed blocking result; branch or HEAD drift becomes an explicit warning with current values.

Do not mutate permissions or create a replacement worktree.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_worktree.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_context_pack.py -k "worktree or exact_cwd or permission_preflight" -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/worktree.py session_bridge/store.py session_bridge/coordinator.py session_bridge/context_pack.py tests/session_bridge/test_worktree.py tests/session_bridge/test_coordinator.py tests/session_bridge/test_context_pack.py
git commit -m "feat(session-bridge): preserve exact continuation worktree"
```

## Task 9: Surface sidebar delivery in the Hermes catalog API and desktop UI

**Files:**

- Modify: `session_bridge/store.py:620-699`
- Modify: `hermes_cli/web_server.py:3816-3865`
- Modify: `tests/session_bridge/test_web_catalog.py`
- Modify: `apps/desktop/src/types/hermes.ts:320-326`
- Modify: `apps/desktop/src/lib/session-source.ts`
- Modify: `apps/desktop/src/lib/session-source.test.ts`
- Modify: `apps/desktop/src/lib/session-search.ts`
- Modify: `apps/desktop/src/lib/session-search.test.ts`
- Modify: `apps/desktop/src/app/chat/sidebar/session-row.tsx:77-90`
- Create: `apps/desktop/src/app/chat/sidebar/session-row.test.tsx`

### Step 1: Write failing public API tests

Add assertions for these allowlisted fields:

```text
bridge_sidebar_state
bridge_sidebar_codex_thread_id
bridge_sidebar_error
bridge_sidebar_stale
```

Assert lease digest/token, signed marker, registration prompt, source native path, and arbitrary errors are never serialized. Internal errors become `delivery_degraded`.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_web_catalog.py -q
```

Expected: FAIL because sidebar summary fields are absent.

### Step 2: Add batched summaries and sanitization

Extend `get_bridge_summaries()` with one batched query over `session_sidebar_jobs`. Map internal states to the public labels without exposing queue internals:

```python
PUBLIC_SIDEBAR_STATE = {
    "sidebar_pending": "pending",
    "sidebar_leased": "pending",
    "sidebar_retry": "retrying",
    "sidebar_visible": "visible",
    "sidebar_failed": "failed",
}
```

The web API retains its allowlist and sanitizes failure to a fixed public code.

### Step 3: Write failing UI tests and implement badges/search

Extend `SessionInfo` with:

```typescript
bridge_sidebar_state?: 'failed' | 'pending' | 'retrying' | 'visible' | null
bridge_sidebar_codex_thread_id?: null | string
bridge_sidebar_error?: 'delivery_degraded' | null
bridge_sidebar_stale?: boolean
```

Render exactly `Pending`, `Visible in Codex`, `Retrying`, or `Failed`. Make searches for `Codex sidebar`, `visible`, `pending`, `retrying`, and `failed` match. Use the existing badge styling; failed is destructive, pending/retrying accent, visible neutral.

Run:

```bash
npm --prefix apps/desktop run test:ui -- src/lib/session-source.test.ts src/lib/session-search.test.ts src/app/chat/sidebar/session-row.test.tsx
npm --prefix apps/desktop run typecheck
```

Expected: PASS after implementation.

### Step 4: Run the complete task verification and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_web_catalog.py -q
npm --prefix apps/desktop run test:ui -- src/lib/session-source.test.ts src/lib/session-search.test.ts src/app/chat/sidebar/session-row.test.tsx
npm --prefix apps/desktop run typecheck
```

Expected: PASS.

Commit:

```bash
git add session_bridge/store.py hermes_cli/web_server.py tests/session_bridge/test_web_catalog.py apps/desktop/src/types/hermes.ts apps/desktop/src/lib/session-source.ts apps/desktop/src/lib/session-source.test.ts apps/desktop/src/lib/session-search.ts apps/desktop/src/lib/session-search.test.ts apps/desktop/src/app/chat/sidebar/session-row.tsx apps/desktop/src/app/chat/sidebar/session-row.test.tsx
git commit -m "feat(session-bridge): show Codex sidebar delivery status"
```

## Task 10: Add broker health, CLI backfill controls, and stale-heartbeat monitoring

**Files:**

- Modify: `session_bridge/store.py`
- Modify: `session_bridge/coordinator.py:890-950`
- Modify: `session_bridge/cli.py:620-840`
- Modify: `tests/session_bridge/test_cli.py`
- Modify: `tests/session_bridge/test_coordinator.py`

### Step 1: Write failing status and bounded-backfill tests

Assert:

- status includes eligible counts by provider, state counts, oldest pending age, last successful heartbeat, redacted last task ID, fixed recent codes, and p50/p95/p99 delivery latency;
- an empty queue is healthy even with no heartbeat;
- nonempty queue with a stale heartbeat becomes degraded after the configured grace period;
- `sidebar-backfill --days 30 --dry-run` is side-effect free;
- `sidebar-backfill --days 30 --apply --limit 10` queues at most ten;
- `--limit 11` is rejected;
- continuous enqueue remains false after backfill;
- no lease or marker material appears in JSON output.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_coordinator.py -k "sidebar_status or sidebar_backfill or heartbeat" -q
```

Expected: FAIL because the controls and metrics are missing.

### Step 2: Implement explicit operational commands

Add:

```text
hermes-session-bridge sidebar-status --json
hermes-session-bridge sidebar-backfill --days 30 --limit 10 --dry-run
hermes-session-bridge sidebar-backfill --days 30 --limit 10 --apply
hermes-session-bridge sidebar-continuous --enable|--disable
```

Persist explicit enable/disable through `hermes_cli.config.save_config()` under `session_bridge.sidebar`; never rewrite `.env`. Backfill only registers jobs. It never creates Codex tasks itself.

Record a broker heartbeat when `session_sidebar_pending` succeeds, including an empty result. Alert only when work is pending and `oldest_pending_age > 60 + grace_seconds`.

### Step 3: Run tests and commit

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_cli.py tests/session_bridge/test_coordinator.py -k "sidebar_status or sidebar_backfill or heartbeat" -q
```

Expected: PASS.

Commit:

```bash
git add session_bridge/store.py session_bridge/coordinator.py session_bridge/cli.py tests/session_bridge/test_cli.py tests/session_bridge/test_coordinator.py
git commit -m "feat(session-bridge): operate sidebar delivery rollout"
```

## Task 11: Prove the end-to-end broker contract under faults

**Files:**

- Modify: `tests/session_bridge/test_end_to_end.py`
- Modify: `tests/session_bridge/test_fault_injection.py`
- Modify: `tests/session_bridge/test_coordinator_safety.py`
- Modify: `tests/session_bridge/test_coordinator_hardening.py`

### Step 1: Add end-to-end scenarios before any live rollout

Build a fake native Codex task harness around the public MCP functions. Cover:

1. Meaningful Claude index -> enqueue -> lease -> native create -> title -> authenticated commit -> visible catalog state.
2. Meaningful Hermes session through the same path.
3. Exact cwd saved-project selection.
4. Exact git-root project selection.
5. `.hermes` Session Inbox fallback with source cwd preserved.
6. Create succeeds, commit connection drops, marker reconciliation finds the exact task, and no duplicate is created.
7. Lease expires before create, and the stale worker cannot commit.
8. Rename fails after create, retry reconciles the task rather than creating another.
9. Claude parser failure does not block Hermes delivery and vice versa.
10. Codex Desktop offline leaves durable pending/retry jobs.
11. No app-server creation call occurs.
12. Empty/control-only/ack-only sessions produce no jobs.

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_end_to_end.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_coordinator_safety.py tests/session_bridge/test_coordinator_hardening.py -k sidebar -q
```

Expected: FAIL until all cross-component wiring is correct.

### Step 2: Make only the minimum integration fixes

Fix boundary issues revealed by the tests. Do not relax marker verification, lease ownership, exact-cwd validation, or provider isolation. Do not add an app-server fallback.

### Step 3: Run focused and full bridge tests

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/ tests/hermes_state/test_session_bridge_schema.py -q
```

Expected: PASS with zero failures.

Commit:

```bash
git add tests/session_bridge/test_end_to_end.py tests/session_bridge/test_fault_injection.py tests/session_bridge/test_coordinator_safety.py tests/session_bridge/test_coordinator_hardening.py
git commit -m "test(session-bridge): prove native sidebar broker flow"
```

If the fault tests require a source correction, add only each named corrected
file after reviewing its diff. Before committing, inspect
`git diff --cached --name-only` and unstage any file not changed for this task.

## Task 12: Install the skill and pass manual native canaries

**Files:**

- Runtime install target: `%CODEX_HOME%/skills/session-sidebar-sync/SKILL.md`
- Runtime config: `%HERMES_HOME%/config.yaml`
- Runtime database: `%HERMES_HOME%/state.db`
- No repository source changes unless a canary reveals a tested defect.

### Step 1: Establish a clean, disabled baseline

Run:

```bash
hermes-session-bridge install-sidebar-skill
hermes-session-bridge sidebar-continuous --disable
hermes-session-bridge sidebar-status --json
```

Expected: the skill path is printed; continuous is false; no secret, lease, marker, or native transcript path appears.

Restart Session Bridge, Codex Desktop, and Hermes Desktop. Run the active MCP health procedure and verify `gbrain`, `mempalace`, and Session Bridge are healthy before any mutation.

### Step 2: Prove an empty manual broker invocation

Invoke `$session-sidebar-sync` manually from the current Codex task with no pending work.

Expected: no new task, no user-facing empty-batch noise, successful broker heartbeat.

### Step 3: Deliver one Claude canary

Create or select one recent native Claude session containing a unique meaningful request. Register only that source, invoke `$session-sidebar-sync`, and verify:

- one `[Claude] ...` task appears directly in the Codex left sidebar without Ctrl+G;
- the linked native Codex task contains one valid signed registration marker;
- the Hermes catalog shows `Visible in Codex`;
- opening and continuing it calls `session_continue` before substantive work;
- a read-only command runs with the exact recorded Claude cwd/worktree;
- branch, HEAD, and uncommitted files match the source worktree;
- repeating the skill does not create a duplicate.

If any check fails, disable sidebar registration, mark the fixed failure code, and stop before Hermes canary/backfill.

### Step 4: Deliver one Hermes canary

Repeat with one unique meaningful native Hermes session. Verify one `[Hermes] ...` sidebar task, exact Hermes cwd, catalog linkage, and duplicate absence.

### Step 5: Record evidence

Save only IDs, timestamps, fixed outcomes, and redacted paths to a versioned rollout record in `session_bridge_state`. Do not store screenshots or raw transcripts in the database.

Expected: both provider canaries are visible and continuable from the exact source locations.

## Task 13: Pass the scheduled-execution canary, then create the one-minute heartbeat

**Files:**

- Codex Desktop scheduled task state only.
- No repository source changes unless a canary reveals a tested defect.

### Step 1: Create one scheduled canary while continuous delivery remains off

Using the Codex Desktop automation tool, create a temporary local scheduled run scoped to the saved `.hermes` project with this prompt:

```text
Invoke $session-sidebar-sync exactly once. If no sidebar jobs are pending, end silently. Do not perform project work or summarize transcripts. Never create a task without a valid lease and never use external app-server creation.
```

Queue exactly one canary job and trigger the schedule once.

Expected: scheduled execution can list projects, create a native task, rename it, and commit it. The task appears in the left sidebar.

If native task creation or renaming is unavailable in scheduled execution, delete/disable the temporary schedule, leave continuous delivery off, and stop. Do not backfill and do not substitute app-server creation.

### Step 2: Prove scheduled ambiguity handling

Inject one controlled post-create commit failure. Trigger the next run after retry becomes due.

Expected: it reconciles the authenticated existing task and creates no duplicate.

### Step 3: Replace the canary with the production heartbeat

Only after Steps 1-2 pass, use the Codex Desktop automation tool to create the production broker task:

- Name: `Session Sidebar Sync`
- Project: saved `.hermes`
- Environment: local
- Interval: every one minute
- Prompt: the exact prompt above
- Initial state: enabled

Verify two consecutive empty heartbeats update health without creating tasks or messages.

## Task 14: Run bounded 30-day backfill and enable continuous delivery after soak

**Files:**

- Runtime config: `%HERMES_HOME%/config.yaml`
- Runtime database: `%HERMES_HOME%/state.db`
- GBrain and MemPalace records per global capture discipline.

### Step 1: Dry-run the 30-day population

Run:

```bash
hermes-session-bridge sidebar-backfill --days 30 --limit 10 --dry-run
```

Review counts by provider and spot-check exclusions for `ok`, `yes`, control-only, automation, subagent, bridge-origin, and empty sessions.

Expected: only meaningful native Claude and Hermes sessions appear; no state changes.

### Step 2: Apply one bounded sample

Run:

```bash
hermes-session-bridge sidebar-backfill --days 30 --limit 10 --apply
```

Wait for the next broker heartbeat. Verify all sample tasks appear in the sidebar, titles have provider prefixes, catalog state is visible, and there are no duplicate bridge IDs or Codex task IDs.

### Step 3: Complete backfill in batches of ten

Repeat the apply command and wait for delivery between batches. Stop immediately if:

- any authenticated marker conflict occurs;
- duplicate task identity is detected;
- error rate exceeds the existing rollout breaker threshold;
- oldest pending age grows while successful heartbeats continue;
- exact-cwd permission preflight fails.

Expected: all eligible sources active in the last 30 days reach `visible` or a fixed reviewed `failed` state.

### Step 4: Soak for 30 minutes

Keep Claude Code, Codex Desktop, and Hermes Desktop open. Observe at least 30 one-minute heartbeats. Verify:

- new eligible Claude and Hermes jobs deliver within 60 seconds under normal local load;
- empty intervals create no noise;
- queues drain;
- no duplicate tasks appear;
- memory MCPs remain healthy;
- provider indexing remains isolated.

### Step 5: Enable continuous registration

Run:

```bash
hermes-session-bridge sidebar-continuous --enable
hermes-session-bridge sidebar-status --json
```

Expected: continuous is true, heartbeat is fresh, queues are healthy, and all status output is sanitized.

### Step 6: Final verification and durable capture

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/ tests/hermes_state/test_session_bridge_schema.py -q
npm --prefix apps/desktop run test:ui -- src/lib/session-source.test.ts src/lib/session-search.test.ts src/app/chat/sidebar/session-row.test.tsx
npm --prefix apps/desktop run typecheck
git status --short
```

Expected: all tests pass; typecheck passes; only intentional runtime state exists outside Git; repository worktree is clean.

Search GBrain, then scoped MemPalace wing `hermes`, and record the shipped behavior, operational task ID, canary evidence, rollback command, and known limitations without secrets or raw transcript content.

## Rollback procedure

1. Disable `Session Sidebar Sync` using the Codex Desktop automation tool.
2. Run `hermes-session-bridge sidebar-continuous --disable`.
3. Leave visible native Codex tasks intact; do not archive or delete automatically.
4. Leave queued jobs durable for diagnosis or mark them with fixed failure codes.
5. Do not change provider indexing, unified catalog, GBrain, or MemPalace.
6. If code rollback is required, revert sidebar commits only; `session_sidebar_jobs` is additive and can remain dormant.

## Acceptance checklist

- [ ] Recent meaningful native Claude sessions appear in the Codex left sidebar.
- [ ] Recent meaningful native Hermes sessions appear in the Codex left sidebar.
- [ ] New eligible sessions normally appear within one minute.
- [ ] Sole acknowledgement/control/empty/automation/subagent sessions do not appear.
- [ ] Native tasks are titled `[Claude] ...` or `[Hermes] ...`.
- [ ] Exact cwd/worktree, branch, HEAD, and uncommitted files are preserved on continuation.
- [ ] Saved-project selection and `.hermes` Session Inbox fallback are deterministic.
- [ ] Ambiguous create/commit outcomes reconcile without duplicates.
- [ ] Hermes catalog shows Pending, Visible in Codex, Retrying, or Failed.
- [ ] No lease token, signed marker, transcript path, raw exception, or secret leaks through public status/API.
- [ ] No Codex database, global UI state, packaged application file, or provider transcript store is mutated directly.
- [ ] No external app-server fallback is present.
- [ ] Manual Claude, manual Hermes, and scheduled native-creation canaries all pass before backfill.
- [ ] Thirty-minute one-minute-heartbeat soak passes before continuous registration is enabled.
