# Session Sidebar Backfill Exclusions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make sidebar dry-run and apply perform the same exact-cwd preflight, durably exclude historical sessions whose cwd is gone, and resume the guarded native-sidebar rollout without duplicate tasks.

**Architecture:** Add an exclusion ledger beside (not inside) the delivery-job state machine. Filter persisted exclusions at the candidate SQL boundary, share worktree preflight between preview/apply/continuous registration, and expose exclusions separately from failures in summaries and status. Keep every unknown identity, permission, database, and infrastructure problem fail-closed.

**Tech Stack:** Python 3.11+, SQLite, dataclasses, asyncio/to_thread, pytest through `scripts/run_tests.sh`, Codex automation API, Session Bridge CLI/MCP.

---

## File map

- `hermes_state.py`: schema version 22 plus additive exclusion table and indexes.
- `session_bridge/store.py`: exclusion validation, idempotent persistence, counts, and candidate-query filtering.
- `session_bridge/worktree.py`: distinguish missing paths from permission/non-missing I/O failures.
- `session_bridge/coordinator.py`: shared preview/apply preflight, exclusion accounting, and health summary fields.
- `session_bridge/cli.py`: preserve exit semantics while serializing exclusion fields.
- `tests/session_bridge/test_store.py`: schema, persistence, conflict, filtering, and status contracts.
- `tests/session_bridge/test_worktree.py`: fixed error classification for missing versus inaccessible paths.
- `tests/session_bridge/test_coordinator.py`: preview/apply parity, side effects, starvation, and unknown-error behavior.
- `tests/session_bridge/test_cli.py`: JSON and exit-code contract.
- `docs/superpowers/specs/2026-07-15-sidebar-backfill-exclusions-design.md`: approved behavior contract; no further edits unless implementation exposes a contradiction.

### Task 1: Add the durable exclusion ledger

**Files:**
- Modify: `hermes_state.py:142`
- Modify: `hermes_state.py:899-977`
- Modify: `session_bridge/store.py:786-910`
- Modify: `session_bridge/store.py:1864-2037`
- Test: `tests/session_bridge/test_store.py`

- [ ] **Step 1: Write failing schema and store tests**

Add tests that open a fresh `SessionDB`, assert the persisted schema version
equals the imported `SCHEMA_VERSION` (never a change-detector literal), record
one exclusion, repeat the exact operation idempotently, reject a conflicting
identity digest, and verify the row contains only bounded audit metadata.

```python
def test_sidebar_exclusion_is_idempotent_and_conflicts_fail_closed(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    candidate = _sidebar_candidate(db, native_id="gone")

    created = store.record_sidebar_exclusion(
        source_session_id=candidate.source_session_id,
        provider=Provider.CLAUDE,
        reason_code="source_cwd_missing",
        now=90.0,
    )
    repeated = store.record_sidebar_exclusion(
        source_session_id=candidate.source_session_id,
        provider=Provider.CLAUDE,
        reason_code="source_cwd_missing",
        now=95.0,
    )

    assert created["created"] is True
    assert repeated["created"] is False
    assert store.sidebar_exclusion_counts() == {
        "total": 1,
        "by_reason": {"source_cwd_missing": 1},
    }
    db._execute_write(lambda conn: conn.execute(
        "UPDATE session_sidebar_exclusions "
        "SET source_identity_digest = ? WHERE source_session_id = ?",
        ("0" * 64, candidate.source_session_id),
    ))
    with pytest.raises(ValueError, match="conflicting sidebar exclusion"):
        store.record_sidebar_exclusion(
            source_session_id=candidate.source_session_id,
            provider=Provider.CLAUDE,
            reason_code="source_cwd_missing",
            now=96.0,
        )
```

Add a query test with a newer excluded source and an older valid source. After
the exclusion is persisted, `list_sidebar_candidates(..., limit=1)` must return
the valid source rather than an empty page or the excluded source.

```python
def test_sidebar_candidate_query_omits_persisted_exclusions(
    db: SessionDB,
) -> None:
    store = SessionBridgeStore(db, clock=lambda: 300.0)
    newer = _sidebar_candidate(db, native_id="newer-gone", eligible_at=200.0)
    older = _sidebar_candidate(db, native_id="older-valid", eligible_at=100.0)
    store.record_sidebar_exclusion(
        source_session_id=newer.source_session_id,
        provider=Provider.CLAUDE,
        reason_code="source_cwd_missing",
        now=300.0,
    )

    page = store.list_sidebar_candidates(0.0, 1)

    assert [source.source_session_id for source in page] == [
        older.source_session_id
    ]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py -k "sidebar_exclusion or candidate_query_omits_persisted" -q
```

Expected: FAIL because schema version 22, `session_sidebar_exclusions`,
`record_sidebar_exclusion`, and `sidebar_exclusion_counts` do not exist.

- [ ] **Step 3: Add schema version 22 and the additive table**

Change `SCHEMA_VERSION` and add the table to `SCHEMA_SQL`:

```python
SCHEMA_VERSION = 22
```

```sql
CREATE TABLE IF NOT EXISTS session_sidebar_exclusions (
    source_session_id TEXT PRIMARY KEY REFERENCES sessions(id),
    provider TEXT NOT NULL CHECK (provider IN ('claude', 'hermes')),
    reason_code TEXT NOT NULL CHECK (reason_code IN ('source_cwd_missing')),
    source_identity_digest TEXT NOT NULL,
    excluded_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_sidebar_exclusions_reason
    ON session_sidebar_exclusions(reason_code, excluded_at DESC);
```

The existing initialization path runs additive `CREATE TABLE IF NOT EXISTS`
DDL before updating `schema_version`, so no destructive migration or row
backfill is required.

- [ ] **Step 4: Implement store validation and idempotent persistence**

Add a fixed reason set and deterministic identity digest:

```python
SIDEBAR_EXCLUSION_REASONS = frozenset({"source_cwd_missing"})


def _sidebar_exclusion_digest(
    source_session_id: str,
    provider: Provider,
    reason_code: str,
) -> str:
    material = "\x00".join((source_session_id, provider.value, reason_code))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
```

Implement `record_sidebar_exclusion` with one `_execute_write` transaction.
Validate the source ID with the existing canonical sidebar identity helper,
require provider `claude` or `hermes`, require the fixed reason, use
`INSERT OR IGNORE`, then read and compare every identity field. Return only
`{"created": bool, "source_session_id": ..., "reason_code": ...}`.

Implement:

```python
def sidebar_exclusion_counts(self) -> dict[str, Any]:
    ...
```

Return `{"total": int, "by_reason": dict[str, int]}` with zero-filled known
reasons.

- [ ] **Step 5: Filter exclusions at the candidate SQL boundary**

In the `source_metadata` CTE, add a second `NOT EXISTS` predicate beside the
delivery-job predicate:

```sql
AND NOT EXISTS (
    SELECT 1
      FROM session_sidebar_exclusions AS sidebar_exclusion
     WHERE sidebar_exclusion.source_session_id = s.id
)
```

Do not filter in Python after pagination; the SQL boundary is what prevents
persisted exclusions from consuming page and examination budgets.

- [ ] **Step 6: Verify GREEN and run store regressions**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py -k "sidebar" -q
```

Expected: all sidebar store tests pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add hermes_state.py session_bridge/store.py tests/session_bridge/test_store.py
git commit -m "feat(session-bridge): persist sidebar exclusions"
```

### Task 2: Separate missing paths from permission and I/O failures

**Files:**
- Modify: `session_bridge/worktree.py:18-60`
- Test: `tests/session_bridge/test_worktree.py`

- [ ] **Step 1: Write failing fixed-code classification tests**

Add one real missing-directory test and narrowly patch `Path.lstat` for the
permission and generic-I/O branches. The assertions target public fixed codes,
not exception text from the OS.

```python
def test_capture_missing_worktree_is_excludable(tmp_path: Path) -> None:
    missing = tmp_path / "deleted-worktree"

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(missing))

    assert raised.value.code == "source_cwd_missing"


@pytest.mark.parametrize("error", [PermissionError(), OSError("io")])
def test_capture_inaccessible_worktree_is_not_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: OSError,
) -> None:
    def _raise_error(path: Path) -> os.stat_result:
        raise error

    monkeypatch.setattr(Path, "lstat", _raise_error)

    with pytest.raises(WorktreeSnapshotError) as raised:
        capture_worktree_snapshot(str(tmp_path))

    assert raised.value.code == "permission_preflight_failed"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_worktree.py -k "missing_worktree_is_excludable or inaccessible_worktree_is_not_missing" -q
```

Expected: the permission/I/O test FAILS because all `OSError` values currently
collapse to `source_cwd_missing`.

- [ ] **Step 3: Implement minimal error separation**

Permit the existing fixed permission code in `WorktreeSnapshotError` and split
the exception branches:

```python
if code not in {
    "source_cwd_missing",
    "source_identity_mismatch",
    "permission_preflight_failed",
}:
    raise ValueError("invalid worktree snapshot error code")
```

```python
try:
    source_lstat = source.lstat()
    resolved = source.resolve(strict=True)
    resolved_stat = resolved.stat()
except (FileNotFoundError, NotADirectoryError):
    raise WorktreeSnapshotError("source_cwd_missing") from None
except (PermissionError, OSError):
    raise WorktreeSnapshotError("permission_preflight_failed") from None
```

Retain the explicit `resolved.is_dir()` missing-path classification and all Git
identity checks.

- [ ] **Step 4: Verify GREEN and all worktree regressions**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_worktree.py -q
```

Expected: all worktree tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add session_bridge/worktree.py tests/session_bridge/test_worktree.py
git commit -m "fix(session-bridge): classify missing worktrees"
```

### Task 3: Share candidate preflight across dry-run and apply

**Files:**
- Modify: `session_bridge/coordinator.py:80-95`
- Modify: `session_bridge/coordinator.py:835-1050`
- Modify: `session_bridge/coordinator.py:1858-1873`
- Test: `tests/session_bridge/test_coordinator.py:2988-3040`

- [ ] **Step 1: Write the preview/apply parity regression test**

Use a temporary Git repository for the valid source and delete a second source
directory after indexing it. Run preview first, assert no writes, then run apply
against stable input.

```python
@pytest.mark.asyncio
async def test_sidebar_backfill_preview_matches_apply_exclusions(
    sidebar_db: SessionDB,
    tmp_path: Path,
) -> None:
    valid = tmp_path / "valid"
    deleted = tmp_path / "deleted"
    _exact_cwd_repo(valid)
    _exact_cwd_repo(deleted)
    store = SessionBridgeStore(sidebar_db, clock=lambda: 3_000_000.0)
    store.upsert_projection(_sidebar_projection(
        provider=Provider.CLAUDE,
        native_id="valid",
        content="Keep this exact worktree",
        last_active=3_000_000.0,
        cwd=str(valid),
    ))
    store.upsert_projection(_sidebar_projection(
        provider=Provider.CLAUDE,
        native_id="deleted",
        content="This historical worktree is gone",
        last_active=2_999_999.0,
        cwd=str(deleted),
    ))
    shutil.rmtree(deleted)
    coordinator = SessionBridgeCoordinator(
        config=_sidebar_config(continuous=False),
        store=store,
        adapters={},
        target_adapters={Provider.CODEX: _ForbiddenSidebarTarget()},
        clock=lambda: 3_000_000.0,
    )

    preview = await coordinator.backfill_sidebar_jobs_once(
        now=3_000_000.0, days=30, limit=10, apply=False
    )

    assert preview.queued == 1
    assert preview.failed == 0
    assert preview.excluded == 1
    assert preview.excluded_by_reason == {"source_cwd_missing": 1}
    assert store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 0
    assert store.sidebar_exclusion_counts()["total"] == 0

    applied = await coordinator.backfill_sidebar_jobs_once(
        now=3_000_000.0, days=30, limit=10, apply=True
    )

    assert asdict(applied) == asdict(preview)
    assert store.sidebar_job_counts()[SidebarJobState.PENDING.value] == 1
    assert store.sidebar_exclusion_counts()["total"] == 1
```

Add separate tests proving:

- `permission_preflight_failed` increments `failed`, not `excluded`;
- `source_identity_mismatch` increments `failed`, not `excluded`;
- an existing pending job is checked before filesystem preflight and is not
  converted to an exclusion;
- more than 40 persisted exclusions do not prevent an older valid source from
  being returned and queued.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py -k "backfill_preview_matches_apply_exclusions or preflight_failed_is_not_excluded or persisted_exclusions_do_not_starve" -q
```

Expected: FAIL because preview does not snapshot worktrees, summary exclusion
fields do not exist, and apply has no durable exclusion write.

Add `import shutil` to `test_coordinator.py` for the real deleted-directory
fixture.

- [ ] **Step 3: Extend the registration summary with safe defaults**

Import `field` from `dataclasses` and extend the frozen dataclass without
breaking existing construction sites:

```python
@dataclass(frozen=True)
class SidebarRegistrationSummary:
    examined: int
    queued: int
    by_provider: Mapping[str, int]
    failed: int
    excluded: int = 0
    excluded_by_reason: Mapping[str, int] = field(default_factory=dict)
```

Initialize `excluded = 0` and a zero-filled mutable local reason counter at the
start of `_register_sidebar_jobs_locked`.

- [ ] **Step 4: Move existing-job lookup before filesystem preflight**

After canonical source validation, call `get_sidebar_job_for_source`. If a job
exists, continue immediately. This preserves every pending/leased/retry/visible
or failed job even if its source directory later disappears.

- [ ] **Step 5: Run one shared snapshot path for preview and apply**

Build the candidate, then unconditionally call `capture_worktree_snapshot`
whenever the store supports snapshot-aware enqueue. Canonicalize candidate cwd
and Git metadata from the snapshot before the `if not apply` branch.

Catch `WorktreeSnapshotError` separately from the generic candidate handler:

```python
except WorktreeSnapshotError as exc:
    if exc.code != "source_cwd_missing":
        failed += 1
        if apply:
            self._record_error_code("sidebar_registration_candidate_failed")
        continue
    try:
        if apply:
            await asyncio.to_thread(
                _call,
                self._store,
                "record_sidebar_exclusion",
                source_session_id=canonical_source,
                provider=projection.provider,
                reason_code=exc.code,
                now=registration_time,
            )
    except Exception:
        failed += 1
        if apply:
            self._record_error_code("sidebar_registration_candidate_failed")
    else:
        excluded += 1
        excluded_by_reason[exc.code] += 1
```

Represent an absent/blank cwd by raising the same fixed
`WorktreeSnapshotError("source_cwd_missing")` inside the candidate scope. Do
not copy raw exception text into health or CLI output.

Ensure `canonical_source` and `projection` are assigned only after validation;
the exclusion handler must not persist malformed identities.

- [ ] **Step 6: Return and record the extended summary**

Populate both new fields in every explicit empty summary and the final summary.
Extend `_set_sidebar_registration_counts` with `excluded` and a shallow copy of
the fixed reason mapping. Do not add exclusions to recent error codes.

- [ ] **Step 7: Verify GREEN and coordinator regressions**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_coordinator.py -k "sidebar" -q
```

Expected: all coordinator sidebar tests pass, including the new preview/apply
parity test.

- [ ] **Step 8: Commit Task 3**

```bash
git add session_bridge/coordinator.py tests/session_bridge/test_coordinator.py
git commit -m "fix(session-bridge): align sidebar preview preflight"
```

### Task 4: Expose exclusions without degrading CLI or broker health

**Files:**
- Modify: `session_bridge/store.py:1864-2018`
- Modify: `session_bridge/cli.py:306-335`
- Test: `tests/session_bridge/test_store.py`
- Test: `tests/session_bridge/test_cli.py:230-305`

- [ ] **Step 1: Write failing status and CLI tests**

Add a status test that persists one exclusion with no jobs and expects:

```python
assert status["counts"]["sidebar_excluded"] == 1
assert status["recent_error_codes"] == []
assert status["oldest_pending_age_seconds"] is None
```

Add CLI parameterization for an exclusion-only preview and apply result:

```python
@pytest.mark.parametrize("mode", ["--dry-run", "--apply"])
def test_sidebar_backfill_exclusions_exit_successfully(
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    backend = FakeBackend(
        sidebar_backfill_payload={
            "examined": 1,
            "queued": 0,
            "by_provider": {"claude": 0, "hermes": 0},
            "failed": 0,
            "excluded": 1,
            "excluded_by_reason": {"source_cwd_missing": 1},
        }
    )

    assert _run(
        ["sidebar-backfill", "--days", "30", "--limit", "10", mode],
        backend,
    ) == 0
    assert _json_output(capsys)["excluded"] == 1
```

Retain the existing test proving `failed == 1` exits 3.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py tests/session_bridge/test_cli.py -k "sidebar_excluded or exclusions_exit_successfully" -q
```

Expected: status lacks `sidebar_excluded`; CLI fixture/output lacks the new
fields.

- [ ] **Step 3: Add the exclusion count to sidebar status**

After `counts = self.sidebar_job_counts()`:

```python
counts["sidebar_excluded"] = self.sidebar_exclusion_counts()["total"]
```

Do not include exclusions in provider job counts, actionable age, delivery
latency, recent error codes, or the public health degradation rules.

- [ ] **Step 4: Preserve CLI semantics and serialize the new fields**

`ProductionBackend.sidebar_backfill` already uses `asdict`, and the command
already degrades only on `failed`. Keep that behavior. Update `FakeBackend`
defaults and assertions so dry-run emits `would_queue`, zeroes `queued`, and
retains `excluded` plus `excluded_by_reason`.

- [ ] **Step 5: Verify GREEN and combined regressions**

Run:

```bash
bash scripts/run_tests.sh tests/session_bridge/test_store.py tests/session_bridge/test_cli.py -k "sidebar" -q
```

Expected: all sidebar store and CLI tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add session_bridge/store.py session_bridge/cli.py tests/session_bridge/test_store.py tests/session_bridge/test_cli.py
git commit -m "feat(session-bridge): report sidebar exclusions"
```

### Task 5: Prove the complete local behavior

**Files:**
- Modify only if a failing regression reveals a direct requirement gap.
- Test: `tests/session_bridge/`

- [ ] **Step 1: Run the focused canonical suite**

Run:

```bash
bash scripts/run_tests.sh \
  tests/session_bridge/test_worktree.py \
  tests/session_bridge/test_store.py \
  tests/session_bridge/test_coordinator.py \
  tests/session_bridge/test_cli.py \
  tests/session_bridge/test_end_to_end.py \
  -q
```

Expected: zero failures.

- [ ] **Step 2: Run static checks on changed Python files**

Run:

```bash
.venv/Scripts/python.exe -m ruff check \
  hermes_state.py \
  session_bridge/store.py \
  session_bridge/worktree.py \
  session_bridge/coordinator.py \
  session_bridge/cli.py \
  tests/session_bridge/test_store.py \
  tests/session_bridge/test_worktree.py \
  tests/session_bridge/test_coordinator.py \
  tests/session_bridge/test_cli.py
```

Run:

```bash
.venv/Scripts/python.exe -m py_compile \
  hermes_state.py \
  session_bridge/store.py \
  session_bridge/worktree.py \
  session_bridge/coordinator.py \
  session_bridge/cli.py
```

Expected: both commands exit 0 with no errors.

- [ ] **Step 3: Review the implementation against the approved spec**

Verify explicitly:

- preview and apply share `capture_worktree_snapshot`;
- only `source_cwd_missing` is persisted;
- persisted exclusions are filtered in SQL before pagination;
- existing jobs win before preflight;
- exclusion-only output exits 0;
- unknown failures still exit 3;
- no provider transcript, Codex state, or native task is mutated by exclusion
  handling.

- [ ] **Step 4: Commit any direct verification-only correction**

If Step 1-3 required a correction, use one focused commit containing only that
correction and its regression test. If no correction was needed, do not create
an empty commit.

### Task 6: Deploy and resume the guarded rollout

**Files/state:**
- Live service rooted at `C:\Users\diego\.hermes`
- Permanent Codex automation `session-sidebar-sync`
- Live SQLite state through Session Bridge CLI/store APIs only

- [ ] **Step 1: Keep the broker paused during deployment**

Verify the permanent automation remains `PAUSED` through the supported Codex
automation API or its read-only automation status. Verify live sidebar counts
still show exactly two pending jobs and zero leased/retry/failed before restart.

- [ ] **Step 2: Restart Session Bridge through its established supervisor path**

Do not kill unrelated Hermes workers and do not edit live SQLite directly.
Wait for both Claude and Codex provider scanners to report current healthy state.

- [ ] **Step 3: Run the corrected dry-run against the paused queue**

Run:

```powershell
.\.venv\Scripts\hermes-session-bridge.exe sidebar-backfill --days 30 --limit 10 --dry-run
```

Expected:

- `failed == 0`;
- deleted/absent cwd records appear under `excluded_by_reason.source_cwd_missing`;
- `would_queue` counts only candidates that pass exact worktree preflight;
- no new delivery job or exclusion row is written.

If any other exclusion reason, failure, retry, duplicate, or provider degradation
appears, leave the broker paused and stop without apply.

- [ ] **Step 4: Resume and drain the two pre-existing pending jobs**

Set `session-sidebar-sync` to `ACTIVE` through the supported Codex automation
API, preserving its one-minute heartbeat and target broker task. Deliver exactly
one lease per broker turn. Do not apply a new batch while either job is pending,
leased, retrying, or failed.

After both jobs are visible, verify every visible task has unique thread,
source, bridge, and idempotency identities. Pause immediately on any retry,
failure, provider degradation, or duplicate.

- [ ] **Step 5: Re-run the clean gate and apply one bounded batch**

Only when pending/leased/retry/failed are all zero, run:

```powershell
.\.venv\Scripts\hermes-session-bridge.exe sidebar-backfill --days 30 --limit 10 --apply
```

Immediately before apply, re-run provider health, sidebar counts, uniqueness,
and the corrected dry-run. Apply may persist known exclusions and queue no more
than ten valid jobs. Keep the permanent broker active to drain the batch one job
per turn.

- [ ] **Step 6: Complete bounded backfill**

Repeat dry-run/review/one-batch apply only after the previous batch has zero
pending, leased, retry, and failed rows. Known `source_cwd_missing` exclusions
are allowed; any unknown failure or duplicate pauses the broker.

- [ ] **Step 7: Final soak and continuous enablement**

When dry-run returns `would_queue == 0` and `failed == 0`, observe at least 30
minutes with both harnesses open. Require:

- no duplicate native tasks;
- no pending/leased/retry/failed rows;
- provider health remains current;
- broker records empty successful cycles.

Then run:

```powershell
.\.venv\Scripts\hermes-session-bridge.exe sidebar-continuous --enable
```

Verify one empty broker cycle and one newly meaningful-session registration path
before declaring rollout complete.

- [ ] **Step 8: Persist the completed rollout checkpoint**

Write a verbatim MemPalace record in wing `hermes`, room
`codex-native-sidebar-rollout`, and update the existing GBrain rollout page.
Record commits, test counts, exclusion totals/reasons, final unique task counts,
soak timestamps, and continuous-registration state.
