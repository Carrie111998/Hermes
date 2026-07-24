# DigestComposer rowid-windowing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace DigestComposer's `timestamp >= ?` full-table SCAN (4.3 s on the live 420 MB event bus) with a rowid-range primary-key seek (26–33 ms), preserving digest content and ordering exactly.

**Architecture:** Add two small read methods to `EventBus` (`head_rowid`, `query_rowid_range`) plus a one-time seed helper (`min_rowid_since`). Rewrite `DigestComposer.compose()` to window by a persisted `last_digest_rowid` watermark instead of `last_digest_at`, snapshotting the bus head *before* reading (which also closes a pre-delivery gap). The gateway state-merge already round-trips the full state dict, so no gateway change is needed.

**Tech Stack:** Python 3.11, stdlib `sqlite3` (WAL), pytest. Spec: `docs/superpowers/specs/2026-07-24-digest-rowid-windowing-design.md`.

## Global Constraints

- Repo `~/.hermes/agent-src`, local-only — **never push**. This worktree is `angry-bell-af62d9`, branch `claude/brave-cerf-f3d8fd`, at `main` tip `f039e007c`.
- Do **not** assume the R61 siblings exist here: `idx_events_priority_ts` and `bus.analyze()` are on unmerged branch `claude/exciting-blackburn-765f75`, NOT on this checkout.
- `EventBus.query()` and its 40+ ad-hoc test callers must remain untouched.
- Query-plan tests MUST assert by concrete plan token (`INTEGER PRIMARY KEY`), and MUST be falsified — confirm the test fails when the predicate degrades to a scan (2026-07-23 rule).
- `docs/superpowers/*` is gitignored; commit spec/plan files with `git add -f`.
- PowerShell 5.1 splits `git commit -m` on embedded quotes — use `git commit -F-` heredocs (as below) or a temp file.
- Preserve `ORDER BY rowid ASC` — this is a pure access-path change, not a semantic one.

---

### Task 1: EventBus rowid read + seed helpers

**Files:**
- Modify: `events/bus.py` (add three methods to `EventBus`, after `query()` which ends at line 386)
- Test: `tests/events/test_bus.py` (new test class `TestRowidWindow`)

**Interfaces:**
- Consumes: existing `EventBus._get_conn()`, `EventBus._row_to_event()`, `EventBus.db_path`.
- Produces (later tasks rely on these exact signatures):
  - `EventBus.head_rowid(self) -> int` — current `MAX(rowid)`, or `0` when the table is empty.
  - `EventBus.query_rowid_range(self, after: int, through: int) -> List[Event]` — events with `after < rowid <= through`, ascending; skip-and-warn on unparseable rows.
  - `EventBus.min_rowid_since(self, timestamp: str) -> Optional[int]` — smallest rowid whose `timestamp >= ?`, or `None` if none match (one-time seed use only).

- [ ] **Step 1: Write the failing tests**

Add to `tests/events/test_bus.py` (the file already imports `sqlite3`, `EventBus`, `EventType`, `Priority`; it has a `bus` fixture — reuse it, do not redefine):

```python
class TestRowidWindow:
    """rowid-range windowing for DigestComposer (replaces timestamp SCAN)."""

    def test_head_rowid_empty_and_populated(self, bus):
        assert bus.head_rowid() == 0
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        assert bus.head_rowid() == 2

    def test_query_rowid_range_is_half_open_lower_closed_upper(self, bus):
        ids = [bus.emit(EventType.JOB_DISCOVERED, "scout", {"i": i})
               for i in range(5)]  # rowids 1..5
        got = bus.query_rowid_range(2, 4)  # rowid 3 and 4
        assert [e.event_id for e in got] == [ids[2], ids[3]]

    def test_query_rowid_range_empty_window(self, bus):
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        assert bus.query_rowid_range(1, 1) == []

    def test_query_rowid_range_plans_as_pk_seek(self, bus):
        for i in range(50):
            bus.emit(EventType.GATEWAY_STARTED, "test", {"i": i})
        conn = sqlite3.connect(str(bus.db_path))
        try:
            plan = " ".join(
                row[3] for row in conn.execute(
                    "EXPLAIN QUERY PLAN SELECT * FROM events "
                    "WHERE rowid > ? AND rowid <= ? ORDER BY rowid ASC",
                    (10, 40),
                )
            )
        finally:
            conn.close()
        assert "SEARCH" in plan, f"expected a seek, got: {plan}"
        assert "SCAN" not in plan, f"unexpected scan: {plan}"
        assert "INTEGER PRIMARY KEY" in plan, (
            f"rowid window must use the PK, not another index: {plan}")

    def test_min_rowid_since(self, bus):
        for i in range(3):
            bus.emit(EventType.JOB_DISCOVERED, "scout", {"i": i})
        all_events = bus.query()  # ordered by rowid ASC
        ts_second = all_events[1].timestamp
        assert bus.min_rowid_since(ts_second) == 2
        assert bus.min_rowid_since("2099-01-01T00:00:00+00:00") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd C:/Users/diego/.hermes/agent-src/.claude/worktrees/angry-bell-af62d9 && python -m pytest tests/events/test_bus.py::TestRowidWindow -v`
Expected: FAIL — `AttributeError: 'EventBus' object has no attribute 'head_rowid'` (and siblings).

- [ ] **Step 3: Implement the three methods**

In `events/bus.py`, immediately after the `query()` method (after line 386, before `checkpoint()`), add:

```python
    def head_rowid(self) -> int:
        """Current maximum rowid (0 when the table is empty).

        Snapshot this BEFORE a windowed read so a write landing mid-read is
        deferred to the next window rather than double-counted or dropped.
        """
        row = self._get_conn().execute("SELECT MAX(rowid) FROM events").fetchone()
        return row[0] if row and row[0] is not None else 0

    def query_rowid_range(self, after: int, through: int) -> List[Event]:
        """Events with ``after < rowid <= through``, ascending.

        Half-open lower / closed upper bound: ``after`` is an exclusive
        watermark (the prior digest's high-water rowid), ``through`` a head
        snapshot taken before reading. Plans as an INTEGER PRIMARY KEY seek —
        the whole point, replacing DigestComposer's timestamp SCAN.

        Mirrors ``query()``'s version-skew tolerance: a producer on newer code
        can write event_types this process hasn't loaded; skip the unparseable
        row + WARN rather than crashing the digest (2026-07-10 regression).
        """
        rows = self._get_conn().execute(
            "SELECT * FROM events WHERE rowid > ? AND rowid <= ? ORDER BY rowid ASC",
            (after, through),
        ).fetchall()
        events: List[Event] = []
        for r in rows:
            try:
                events.append(self._row_to_event(r))
            except ValueError as e:
                logger.warning("query_rowid_range: skipping unparseable event %s: %s",
                               r["event_id"], e)
        return events

    def min_rowid_since(self, timestamp: str) -> Optional[int]:
        """Smallest rowid whose ``timestamp >= ?`` (or None).

        One-time seed helper: on the first digest after deploy, translate the
        legacy ``last_digest_at`` timestamp watermark into a rowid floor. This
        is the only remaining timestamp-based scan and never repeats once
        ``last_digest_rowid`` is persisted.
        """
        row = self._get_conn().execute(
            "SELECT MIN(rowid) FROM events WHERE timestamp >= ?",
            (timestamp,),
        ).fetchone()
        return row[0] if row and row[0] is not None else None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/events/test_bus.py::TestRowidWindow -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Falsify the plan test (mandatory, per 2026-07-23 rule)**

Temporarily edit `test_query_rowid_range_plans_as_pk_seek`'s EXPLAIN string to `"...WHERE timestamp >= ? ORDER BY rowid ASC"` with param `("2026-01-01",)` and re-run just that test.
Expected: FAIL — plan shows `SCAN events`, so `assert "SCAN" not in plan` trips. This proves the assertion is non-vacuous. **Revert the edit** and confirm PASS again.

- [ ] **Step 6: Commit**

```bash
git add events/bus.py tests/events/test_bus.py
git commit -F- <<'EOF'
feat(events): add rowid-range read + seed helpers to EventBus

head_rowid / query_rowid_range / min_rowid_since. query_rowid_range
plans as an INTEGER PRIMARY KEY seek (pinned by name, falsified) —
the seek that replaces DigestComposer's timestamp SCAN. Skip-and-warn
version-skew tolerance mirrors query().

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 2: DigestComposer rowid watermark

**Files:**
- Modify: `events/subscribers/digest_composer.py` (`__init__` lines 36–49; `compose()` lines 57–89)
- Test: `tests/events/test_digest_composer.py` (new test class `TestRowidWatermark`)

**Interfaces:**
- Consumes: `EventBus.head_rowid()`, `EventBus.query_rowid_range()`, `EventBus.min_rowid_since()` from Task 1; existing `load_state`/`save_state`, `digest_state_path`.
- Produces: `digest_state.json` gains a `last_digest_rowid: int` field; `DigestComposer._last_digest_rowid: Optional[int]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/events/test_digest_composer.py` (already imports `json`, `datetime/timedelta/timezone`, `Path`, `patch`, `EventBus`, `EventType`, `DigestComposer`, and defines the `bus` fixture). Add `import time` and the state helpers at top of the new class as shown:

```python
class TestRowidWatermark:
    """compose() windows by a persisted rowid watermark, not by wall-clock."""

    def _composer(self, bus, tmp_path):
        # notifier_snapshot_path points nowhere so the digest is event-only.
        return DigestComposer(bus, notifier_snapshot_path=tmp_path / "no-snap.json")

    def test_second_compose_excludes_first_window(self, bus, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        composer = self._composer(bus, tmp_path)
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "A"})
        d1 = composer.compose()
        assert "1 new jobs found" in d1
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "B"})
        d2 = composer.compose()
        assert "1 new jobs found" in d2  # only B, not A+B

    def test_empty_window_still_advances_watermark(self, bus, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from events.paths import digest_state_path
        from events.state import load_state
        composer = self._composer(bus, tmp_path)
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        composer.compose()
        d2 = composer.compose()
        assert "No activity" in d2
        state = load_state(digest_state_path(), default={})
        assert state["last_digest_rowid"] == bus.head_rowid()

    def test_first_run_seeds_floor_from_last_digest_at(self, bus, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from events.paths import digest_state_path
        from events.state import save_state
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "old"})
        time.sleep(0.002)  # guarantee a strictly later timestamp for 'new'
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "new"})
        new = bus.query()[1]
        # State from before this change: a timestamp watermark, no rowid.
        save_state(digest_state_path(), {"last_digest_at": new.timestamp})
        composer = self._composer(bus, tmp_path)
        d = composer.compose()
        assert "1 new jobs found" in d  # floor derived from ts excludes 'old'

    def test_window_is_rowid_not_walltime(self, bus, tmp_path, monkeypatch):
        """Gap fix: once the rowid watermark is set, a stale/backward
        last_digest_at must not gate inclusion. Old code stamped
        last_digest_at=now() before delivery, dropping any event with an
        earlier timestamp forever."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        composer = self._composer(bus, tmp_path)
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        composer.compose()  # last_digest_rowid = 1
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})  # rowid 2
        composer._last_digest_at = "2099-01-01T00:00:00+00:00"  # far future
        d = composer.compose()
        assert "1 new jobs found" in d  # rowid>1 picks up event 2 regardless
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/events/test_digest_composer.py::TestRowidWatermark -v`
Expected: FAIL — `test_empty_window_still_advances_watermark` raises `KeyError: 'last_digest_rowid'`; `test_second_compose_excludes_first_window` FAILS with `"2 new jobs found"` (current code re-reports everything since the timestamp watermark).

- [ ] **Step 3: Update `__init__` to load the rowid watermark**

In `events/subscribers/digest_composer.py`, in `__init__` after line 48 (`self._last_digest_at = state.get("last_digest_at")`), add:

```python
        self._last_digest_rowid: Optional[int] = state.get("last_digest_rowid")
```

- [ ] **Step 4: Rewrite `compose()` windowing + add the floor helper**

Replace the body of `compose()` lines 62–65 (from `query_since = ...` through the `save_state(...)` call) with:

```python
        through = self.bus.head_rowid()  # snapshot head BEFORE reading (gap-free)
        after = self._resolve_floor(since)
        events = self.bus.query_rowid_range(after, through)
        now_iso = datetime.now(timezone.utc).isoformat()
        self._last_digest_rowid = through
        self._last_digest_at = now_iso
        save_state(
            digest_state_path(),
            {"last_digest_at": now_iso, "last_digest_rowid": through},
        )
```

Then add this helper method directly after `compose()` (before `_load_notifier_snapshot`):

```python
    def _resolve_floor(self, since: Optional[str]) -> int:
        """Lower rowid bound (exclusive) for the next digest window.

        Priority: an explicit timestamp override (tests / manual calls) →
        the persisted rowid watermark → first-run seed derived once from the
        legacy ``last_digest_at`` timestamp → 0 (whole bus).
        """
        if since is not None:
            return self._floor_from_timestamp(since)
        if self._last_digest_rowid is not None:
            return self._last_digest_rowid
        if self._last_digest_at:
            return self._floor_from_timestamp(self._last_digest_at)
        return 0

    def _floor_from_timestamp(self, timestamp: str) -> int:
        """Translate a timestamp watermark into an exclusive rowid floor.

        ``min_rowid_since`` returns the first rowid at-or-after the timestamp;
        subtract 1 so ``rowid > floor`` includes that boundary row. Falls back
        to 0 (include everything) when nothing matches.
        """
        min_rowid = self.bus.min_rowid_since(timestamp)
        return (min_rowid - 1) if min_rowid is not None else 0
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `python -m pytest tests/events/test_digest_composer.py::TestRowidWatermark -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the pre-existing digest tests (no regression)**

Run: `python -m pytest tests/events/test_digest_composer.py -v`
Expected: PASS — `TestDigestComposer`, `TestNotifierSnapshotHandshake`, and the new class all green. (The existing `test_compose_from_events` etc. call `compose()` with no prior state → `_last_digest_rowid` is None, `_last_digest_at` is None → floor 0 → whole bus, same as before.)

- [ ] **Step 7: Commit**

```bash
git add events/subscribers/digest_composer.py tests/events/test_digest_composer.py
git commit -F- <<'EOF'
perf(events): window the digest by rowid, not a timestamp SCAN

compose() now snapshots the bus head and reads rowid > watermark via
query_rowid_range (26-33ms PK seek vs 4.3s SCAN on the live 420MB bus).
last_digest_rowid persists alongside last_digest_at; first run after
deploy seeds the floor once from the existing timestamp watermark.
Snapshotting head before the read closes the pre-delivery gap where an
event stamped before last_digest_at=now() was dropped forever.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

### Task 3: State round-trips through the gateway merge

**Files:**
- Test: `tests/events/test_restart_semantics.py` (add one test mirroring `test_poll_loop_preserves_last_digest_at_across_fire`)

**Interfaces:**
- Consumes: `last_digest_rowid` field written by Task 2; existing `gateway_integration` merge behaviour (reload state → set `fired_digest_keys` → save) at `events/gateway_integration.py:673-677`. **No production change** — this task proves the field survives the existing merge, guarding against a future regression that drops it.

- [ ] **Step 1: Write the failing test**

Add to `tests/events/test_restart_semantics.py` (it already imports `load_state`, `save_state` and `gateway_integration as gi`):

```python
def test_poll_loop_preserves_last_digest_rowid_across_fire(tmp_path, monkeypatch):
    """The gateway's post-compose state merge (reload -> set fired_digest_keys
    -> save) must retain last_digest_rowid, or the next compose() would fall
    back to seeding from last_digest_at on every restart. Mirrors the
    last_digest_at guard above; the rowid watermark now shares that path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from events.paths import digest_state_path

    save_state(digest_state_path(), {
        "fired_digest_keys": ["2026-04-19-08"],
        "last_digest_at": "2026-04-19T12:00:00+00:00",
        "last_digest_rowid": 100,
    })

    state_at_thread_start = load_state(digest_state_path(), default={})
    fired_digest_keys = list(state_at_thread_start.get("fired_digest_keys", []))

    # compose() overwrites the file with the new watermark (both fields).
    save_state(digest_state_path(), {
        "last_digest_at": "2026-04-19T17:00:00+00:00",
        "last_digest_rowid": 250,
    })

    # Poll loop reloads FIRST, then re-sets fired_digest_keys and saves.
    fired_digest_keys.append("2026-04-19-13")
    merged = load_state(digest_state_path(), default={})
    merged["fired_digest_keys"] = fired_digest_keys
    merged.pop("last_digest_key", None)
    save_state(digest_state_path(), merged)

    final = load_state(digest_state_path(), default={})
    assert final.get("last_digest_rowid") == 250, (
        "Gateway merge dropped last_digest_rowid — compose() would re-seed "
        "from last_digest_at on every restart")
    assert final.get("fired_digest_keys") == ["2026-04-19-08", "2026-04-19-13"]
```

- [ ] **Step 2: Run the test to verify it passes (merge already correct)**

Run: `python -m pytest tests/events/test_restart_semantics.py::test_poll_loop_preserves_last_digest_rowid_across_fire -v`
Expected: PASS. (This is a characterization test — the existing merge reloads the full dict, so the field survives. It fails only if someone later changes the merge to write a partial dict.)

- [ ] **Step 3: Prove the test is non-vacuous**

Temporarily change the test's final `save_state(digest_state_path(), merged)` to `save_state(digest_state_path(), {"fired_digest_keys": fired_digest_keys})` (a partial-dict merge that drops the rowid).
Expected: FAIL — `assert final.get("last_digest_rowid") == 250` trips (gets `None`). **Revert** and confirm PASS. This shows the test actually guards the merge.

- [ ] **Step 4: Run the full events restart + digest suites**

Run: `python -m pytest tests/events/test_restart_semantics.py tests/events/test_digest_composer.py tests/events/test_bus.py -v`
Expected: PASS — all green, no regression across the three files this change touches.

- [ ] **Step 5: Commit**

```bash
git add tests/events/test_restart_semantics.py
git commit -F- <<'EOF'
test(events): pin last_digest_rowid survival through the gateway merge

Characterization guard: the post-compose reload-then-set-fired_keys
merge must retain the new rowid watermark, else compose() re-seeds from
last_digest_at on every restart. Falsified against a partial-dict merge.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
```

---

## Self-Review

**Spec coverage:**
- rowid-range seek replacing the SCAN → Task 1 (`query_rowid_range`) + Task 2 (`compose` rewrite). ✓
- head-snapshot-before-read (gap fix) → Task 2 Step 4 + `test_window_is_rowid_not_walltime`. ✓
- `last_digest_rowid` persisted alongside `last_digest_at` → Task 2 Step 4. ✓
- First-run seed from `last_digest_at` (choice b) → Task 2 `_resolve_floor`/`_floor_from_timestamp` + `test_first_run_seeds_floor_from_last_digest_at`. ✓
- `query()` untouched → confirmed; new methods are additive. ✓
- Plan pinned by name + falsified → Task 1 Steps 4–5. ✓
- State round-trips gateway merge → Task 3. ✓
- No new index / no ANALYZE (non-goals) → nothing added to `_SCHEMA_SQL`. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code; every run step shows the command and expected result. ✓

**Type consistency:** `head_rowid() -> int`, `query_rowid_range(after: int, through: int) -> List[Event]`, `min_rowid_since(timestamp: str) -> Optional[int]`, `_resolve_floor(since: Optional[str]) -> int`, `_floor_from_timestamp(timestamp: str) -> int`, state field `last_digest_rowid: int`. Names used identically across Tasks 1→2→3. ✓
