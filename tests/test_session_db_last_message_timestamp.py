"""Unit tests for ``SessionDB.get_last_message_timestamp``.

This is the durable freshness signal the gateway auto-resume gate reads
(``gateway.run._resume_transcript_marker_ts``).  It must answer "when did we
last do anything on this transcript" from the messages table — the in-memory
routing markers it replaced were reset to boot time on every gateway restart,
which let a six-week-old interrupted session auto-resume (2026-08-18 incident).
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "freshness.db")
    yield session_db
    session_db.close()


def _session(db, sid="s1"):
    db.create_session(session_id=sid, source="telegram", model="test-model")
    return sid


def test_returns_newest_timestamp(db):
    sid = _session(db)
    db.append_message(sid, "user", content="first", timestamp=1000.0)
    db.append_message(sid, "assistant", content="second", timestamp=3000.5)
    db.append_message(sid, "user", content="third", timestamp=2000.0)

    # MAX, not last-inserted — out-of-order writes must not fool the gate.
    assert db.get_last_message_timestamp(sid) == pytest.approx(3000.5)


def test_returns_none_for_session_without_messages(db):
    sid = _session(db)
    assert db.get_last_message_timestamp(sid) is None


def test_returns_none_for_unknown_session(db):
    assert db.get_last_message_timestamp("no-such-session") is None


def test_is_scoped_to_the_requested_session(db):
    old = _session(db, "old-session")
    new = _session(db, "new-session")
    db.append_message(old, "user", content="six weeks ago", timestamp=1000.0)
    db.append_message(new, "user", content="just now", timestamp=9000.0)

    # A neighbouring fresh session must never make a stale one look fresh.
    assert db.get_last_message_timestamp(old) == pytest.approx(1000.0)
    assert db.get_last_message_timestamp(new) == pytest.approx(9000.0)


def test_counts_inactive_and_compacted_rows(db):
    """Compaction must not reset the transcript clock.

    Compressed/inactive rows still represent work done on this transcript. If
    they were excluded, a heavily compacted-but-recent session could look
    ancient and its legitimate resume would be refused.
    """
    sid = _session(db)
    db.append_message(sid, "user", content="old", timestamp=1000.0)
    mid = db.append_message(sid, "assistant", content="recent", timestamp=8000.0)
    with db._lock:
        db._conn.execute(
            "UPDATE messages SET active = 0, compacted = 1 WHERE id = ?", (mid,)
        )
        db._conn.commit()

    assert db.get_last_message_timestamp(sid) == pytest.approx(8000.0)


def test_never_raises_on_a_closed_db(db):
    """The probe is best-effort: a broken handle degrades to None, not a crash.

    Gateway startup recovery calls this; an exception here would break boot.
    """
    sid = _session(db)
    db.append_message(sid, "user", content="hi", timestamp=1000.0)
    db.close()

    assert db.get_last_message_timestamp(sid) is None
