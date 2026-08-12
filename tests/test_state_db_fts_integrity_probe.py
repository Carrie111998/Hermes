"""Wiring the rank=1 FTS integrity-check into the health paths, bounded.

``check_fts_integrity`` (schema v32) is the only probe that detects an index
rowid whose ``messages`` row is gone: ``PRAGMA integrity_check`` passes,
message writes pass, and ``search_messages`` INNER JOINs the orphan away
rather than failing on it. So it belongs in a health path.

It is also the most expensive statement either health path can run — rank=1
re-reads and re-tokenises every indexed row (12.6s on a 171 MB index; the
production state.db carries ~8x that text). ``hermes doctor`` already has a
documented hang from an unbounded scan on that database, so the rule these
tests pin is: the deep check is opt-in, and wherever it is bounded, running
out of budget must degrade to "unknown" rather than to "corrupt".

That last distinction is not cosmetic. ``conn.interrupt()`` aborts the check
with ``OperationalError: interrupted`` — a ``DatabaseError``, so the naive
handler reports it as a per-table corruption reason, and a caller wired to
``--fix`` would then rebuild a perfectly healthy index.
"""
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import pytest

import hermes_state
from hermes_state import (
    SessionDB,
    StateDbProbeTimeout,
    _db_opens_cleanly,
    _fts_integrity_reason,
    _is_interrupted_error,
    check_state_db_fts_integrity,
)


@pytest.fixture(autouse=True)
def _no_trigram(monkeypatch):
    monkeypatch.setenv("HERMES_DISABLE_MESSAGE_TRIGRAM", "1")


def _seed(db_path: Path, count: int = 10) -> str:
    db = SessionDB(db_path=db_path)
    sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
    for i in range(count):
        db.append_message(sid, role="user", content=f"hello world {i}")
    db.close()
    return sid


def _orphan(db: SessionDB, message_id: int = 3) -> None:
    """Delete a messages row behind the index's back, leaving an orphan.

    The delete trigger is restored verbatim so the trigger count never drops
    and ``_init_schema``'s repair path stays out of the test.
    """
    ddl = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='messages_fts_delete'"
    ).fetchone()[0]
    db._conn.execute("DROP TRIGGER messages_fts_delete")
    db._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    db._conn.execute(ddl)
    db._conn.commit()


# ── the probe detects what the cheap checks cannot ──────────────────────────


def test_cheap_probe_misses_the_orphan(tmp_path):
    """Without the deep check, an orphaned index rowid reads as healthy.

    This is the whole justification for wiring rank=1 in at all: every other
    signal available to a health path says the database is fine.
    """
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        _orphan(db)
    finally:
        db.close()

    assert _db_opens_cleanly(db_path) is None, (
        "the default probe must not start reporting orphans — the repair "
        "paths gate destructive strategies on this result"
    )


def test_deep_probe_reports_the_orphan(tmp_path):
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        _orphan(db)
    finally:
        db.close()

    reason = _db_opens_cleanly(db_path, include_fts_integrity=True)
    assert reason is not None
    assert "messages_fts" in reason


def test_deep_probe_passes_a_healthy_db(tmp_path):
    db_path = tmp_path / "state.db"
    _seed(db_path)
    assert _db_opens_cleanly(db_path, include_fts_integrity=True) is None


def test_deep_probe_is_clean_after_rebuild(tmp_path):
    """'rebuild' is the documented repair; the probe must agree it worked."""
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        _orphan(db)
        assert _fts_integrity_reason(db._conn) is not None
        db.rebuild_fts()
        assert _fts_integrity_reason(db._conn) is None
    finally:
        db.close()


def test_no_fts_tables_is_not_a_failure(tmp_path):
    """An index-less database has nothing to verify, not something wrong."""
    db_path = tmp_path / "plain.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    conn.commit()
    try:
        assert _fts_integrity_reason(conn) is None
    finally:
        conn.close()


# ── the standalone probe doctor calls ──────────────────────────────────────


def test_standalone_probe_reports_health_and_damage(tmp_path):
    db_path = tmp_path / "state.db"
    _seed(db_path)
    assert check_state_db_fts_integrity(db_path) is None

    db = SessionDB(db_path=db_path)
    try:
        _orphan(db)
    finally:
        db.close()

    reason = check_state_db_fts_integrity(db_path)
    assert reason is not None and "messages_fts" in reason


def test_standalone_probe_does_not_run_pragma_integrity_check(tmp_path, monkeypatch):
    """It must not inherit the O(database-size) scan it was split away from.

    The split exists because the two checks scale differently — the FTS check
    is CPU-bound (~70s on the 4.9 GB production snapshot) while PRAGMA
    integrity_check is fragmentation-bound (116.8s compacted, >12 min on the
    live file). Folding them back together would put the cheap answer behind
    the expensive one again.
    """
    db_path = tmp_path / "state.db"
    _seed(db_path)

    seen = []
    real_connect = sqlite3.connect

    class _RecordingConn(sqlite3.Connection):
        def execute(self, sql, *a, **k):
            seen.append(sql)
            return super().execute(sql, *a, **k)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *a, **k: real_connect(*a, **{**k, "factory": _RecordingConn}),
    )
    assert check_state_db_fts_integrity(db_path) is None

    assert seen, "the probe issued no statements"
    assert not any("integrity_check" in s for s in seen), (
        f"the standalone FTS probe ran a page scan: {seen}"
    )
    assert any("integrity-check" in s for s in seen), "the FTS check never ran"


# ── an abort is "unknown", never "corrupt" ─────────────────────────────────


def test_is_interrupted_error_recognises_the_abort():
    assert _is_interrupted_error(sqlite3.OperationalError("interrupted"))
    assert not _is_interrupted_error(
        sqlite3.DatabaseError("database disk image is malformed")
    )


class _AbortingConn:
    """A connection whose integrity-check always aborts.

    ``sqlite3.Connection.execute`` is read-only, so the abort is injected by
    standing in for the connection rather than patching it. Both helpers
    under test touch nothing else on it.
    """

    def __init__(self, tables=("messages_fts",)):
        self._tables = tables
        self.attempts = []

    def execute(self, sql, *args, **kwargs):
        if "integrity-check" in sql:
            self.attempts.append(sql)
            raise sqlite3.OperationalError("interrupted")
        if sql.startswith("SELECT 1 FROM "):
            table = sql.split()[3]
            if table in self._tables:
                return self
            raise sqlite3.OperationalError(f"no such table: {table}")
        raise AssertionError(f"unexpected statement: {sql}")

    def close(self):
        pass


def test_fts_integrity_reason_reraises_an_abort():
    """It must not fold "interrupted" into the reason string.

    A reason string means damage to every caller of this helper, and under
    ``--fix`` damage means a rebuild. An aborted check found nothing.
    """
    conn = _AbortingConn()
    with pytest.raises(sqlite3.OperationalError):
        _fts_integrity_reason(conn)
    assert conn.attempts, "the check never ran"


def test_check_fts_integrity_converts_an_abort_to_a_timeout(tmp_path, monkeypatch):
    """The public method reports the budget miss as StateDbProbeTimeout.

    Deterministic on purpose: the watchdog is forced to have fired, and the
    statement is forced to abort, so this pins the *classification* rather
    than racing a real deadline.
    """
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        fired = threading.Event()
        fired.set()
        monkeypatch.setattr(
            hermes_state,
            "_arm_probe_deadline",
            lambda conn, timeout: (lambda: None, fired),
        )
        monkeypatch.setattr(db, "_conn", _AbortingConn())

        with pytest.raises(StateDbProbeTimeout) as excinfo:
            db.check_fts_integrity(timeout_seconds=5.0)
        assert "integrity-check" in str(excinfo.value)
    finally:
        db.close()


def test_check_fts_integrity_unbounded_still_reports_corruption(tmp_path):
    """The no-budget contract is unchanged: a dict, not an exception."""
    db_path = tmp_path / "state.db"
    _seed(db_path)
    db = SessionDB(db_path=db_path)
    try:
        assert db.check_fts_integrity() == {"messages_fts": None}
        _orphan(db)
        report = db.check_fts_integrity()
        assert report["messages_fts"] is not None
        assert "malformed" in report["messages_fts"]
    finally:
        db.close()


def test_a_real_deadline_aborts_the_check(tmp_path):
    """End-to-end: a live rank=1 really is interruptible.

    The bound is only worth having if ``conn.interrupt()`` reaches inside
    FTS5's integrity-check — it is a virtual-table command driving its own
    prepared statements, not a plain VDBE loop, so this is verified rather
    than assumed. Seeded large enough that the check cannot plausibly finish
    inside the budget.

    The proof is the *outcome*, not a stopwatch. SQLite raises ``interrupted``
    only for a statement it actually cut short: the same call returns a clean
    report unbounded and raises under a budget, so the abort demonstrably
    reached the statement. An earlier draft asserted
    ``elapsed < unbounded / 2`` and failed on a loaded machine at 1.68s
    against a 1.34s baseline — the watchdog thread was simply starved, which
    says nothing about whether the abort worked.
    """
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        sid = db.create_session(session_id=str(uuid.uuid4()), source="cli")
        with db._lock:
            db._conn.execute("BEGIN")
            db._conn.executemany(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', ?, ?)",
                [
                    (sid, f"alpha bravo charlie delta echo foxtrot {i} " * 12, time.time())
                    for i in range(25_000)
                ],
            )
            db._conn.execute("COMMIT")
        db.rebuild_fts()

        unbounded_start = time.perf_counter()
        assert db.check_fts_integrity() == {"messages_fts": None}
        unbounded = time.perf_counter() - unbounded_start
        if unbounded < 0.05:
            pytest.skip(f"machine too fast to time-box reliably ({unbounded:.3f}s)")

        # Budget derived from the measured cost rather than a fixed constant:
        # a fast machine finished the 25k-row check inside a hardcoded 0.1s
        # and skipped this test entirely, which is the failure mode where a
        # guard quietly stops guarding.
        with pytest.raises(StateDbProbeTimeout):
            db.check_fts_integrity(timeout_seconds=max(0.005, unbounded / 20))

        # And the connection survives it: the aborted statement left no
        # transaction or error state behind, so the next check runs clean.
        assert db.check_fts_integrity() == {"messages_fts": None}
    finally:
        db.close()
