import time

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def _last_read(db, sid):
    row = db._conn.execute(
        "SELECT last_read_at FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    return row["last_read_at"] if row is not None else None


def _row(db, sid):
    rows = db.list_sessions_rich(include_archived=True)
    return next(s for s in rows if s["id"] == sid)


def test_untracked_sessions_are_read(db):
    """NULL watermark = never tracked = read, so shipping the column doesn't
    badge a user's entire pre-feature history at once."""
    db.create_session(session_id="s1", source="cli")
    db.append_message(session_id="s1", role="user", content="hi")

    assert _last_read(db, "s1") is None
    assert _row(db, "s1")["unread"] is False


def test_mark_read_then_new_activity_flips_back_to_unread(db):
    db.create_session(session_id="s1", source="cli")
    db.append_message(session_id="s1", role="user", content="hi")

    assert db.set_session_read("s1") is True
    assert _row(db, "s1")["unread"] is False

    # New activity postdating the watermark makes it unread again without
    # any write on the message path.
    time.sleep(0.01)
    db.append_message(session_id="s1", role="assistant", content="reply")
    assert _row(db, "s1")["unread"] is True


def test_mark_unread_explicitly(db):
    db.create_session(session_id="s1", source="cli")
    db.append_message(session_id="s1", role="user", content="hi")
    db.set_session_read("s1")

    assert db.set_session_read("s1", read=False) is True
    assert _last_read(db, "s1") == 0.0
    assert _row(db, "s1")["unread"] is True


def test_missing_session_returns_false(db):
    assert db.set_session_read("nope") is False


def _compression_pair(db: SessionDB):
    base = time.time() - 100
    db.create_session("root", source="cli")
    db.create_session("tip", source="cli", parent_session_id="root")
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, ended_at = ?, end_reason = 'compression', message_count = 1 WHERE id = 'root'",
        (base, base + 10),
    )
    db._conn.execute(
        "UPDATE sessions SET started_at = ?, message_count = 1 WHERE id = 'tip'",
        (base + 20,),
    )
    db._conn.commit()


def test_reading_compression_tip_stamps_whole_lineage(db):
    _compression_pair(db)

    assert db.set_session_read("tip") is True

    root_read = _last_read(db, "root")
    assert root_read is not None and root_read > 0
    assert root_read == _last_read(db, "tip")

    # The projected conversation row (root surfaced as tip) derives read.
    rows = db.list_sessions_rich(order_by_last_active=True)
    assert [s["id"] for s in rows] == ["tip"]
    assert rows[0]["unread"] is False


def test_marking_root_unread_marks_projected_conversation(db):
    _compression_pair(db)
    db.set_session_read("tip")

    assert db.set_session_read("root", read=False) is True

    rows = db.list_sessions_rich(order_by_last_active=True)
    assert [s["id"] for s in rows] == ["tip"]
    assert rows[0]["unread"] is True


def test_mark_read_is_idempotent_when_watermark_already_current(db):
    """Rapid sidebar navigation in the desktop client fires one
    set_session_read per selection. When the caller passes the same
    stamp_at (e.g. by debouncing or by trusting a single source-of-truth
    clock), repeated calls must be no-ops on disk so they do not churn
    SQLite writes. The default `time.time()` path is allowed to advance
    on every call — a strictly later stamp IS an update worth landing."""
    db.create_session(session_id="s1", source="cli")

    stamp = 1_700_000_000.0
    assert db.set_session_read("s1", stamp_at=stamp) is True
    first = _last_read(db, "s1")
    assert first == stamp

    # Same stamp — no row should change.
    assert db.set_session_read("s1", stamp_at=stamp) is False
    assert _last_read(db, "s1") == first

    # Earlier stamp — also no-op (never rewind the watermark).
    assert db.set_session_read("s1", stamp_at=stamp - 1.0) is False
    assert _last_read(db, "s1") == first

    # Later stamp — DOES advance the watermark. This is the steady-state
    # behaviour for real desktop usage where each open picks a fresh
    # `time.time()` and we still want the most recent activity recorded.
    later = stamp + 5.0
    assert db.set_session_read("s1", stamp_at=later) is True
    assert _last_read(db, "s1") == later


def test_mark_unread_is_not_idempotent(db):
    """Marking unread is a deliberate user action and must always land,
    even when the row is already at the unread stamp (0.0)."""
    db.create_session(session_id="s1", source="cli")

    db.set_session_read("s1", read=False)
    assert _last_read(db, "s1") == 0.0

    # Re-applying unread must still report a change — the write is the
    # signal the user took the action, idempotence here would mask
    # repeated user clicks.
    assert db.set_session_read("s1", read=False) is True
    assert _last_read(db, "s1") == 0.0


def test_mark_read_idempotent_on_compression_lineage(db):
    """Idempotence holds per lineage row, not per call site: a second
    stamp at the same value must skip every lineage member that already
    postdates the new value."""
    _compression_pair(db)

    stamp = 1_700_000_000.0
    assert db.set_session_read("tip", stamp_at=stamp) is True
    root_after_first = _last_read(db, "root")
    tip_after_first = _last_read(db, "tip")
    assert root_after_first == stamp
    assert tip_after_first == stamp

    # Same instant — no lineage row should change.
    assert db.set_session_read("tip", stamp_at=stamp) is False
    assert _last_read(db, "root") == root_after_first
    assert _last_read(db, "tip") == tip_after_first
