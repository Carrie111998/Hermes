"""Open rows the routing index can never reach.

``_session_expiry_watcher`` walks ``session_store._entries`` — the
``gateway_routing`` index, keyed by ``session_key``. A row with a NULL key, or
one whose key was pruned or belongs to a previous gateway process, is not in
that index, so its reset policy is never evaluated and it stays open forever.
``prune``/``archive`` cannot reach it either: their selector is pinned to
``ended_at IS NOT NULL``.

Observed on a single-user install: 703 open rows, 551 of them ``api_server``
with a NULL key, and Matrix rows idle 69 days under a 120-minute idle policy.

These tests pin the selector and — more importantly — the rows it must refuse
to touch. Unlike ``list_never_active_keyed_sessions`` this one deliberately
DOES claim rows that carry a transcript, so the "refuses" cases matter more.
"""

import time

import pytest

from hermes_state import SessionDB

DAY = 86400.0


@pytest.fixture()
def db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _sessions_columns(db) -> frozenset:
    """Real column set, read from the schema.

    A hand-maintained list drifts: the first version of it omitted
    `end_reason` and broke a test that had been passing.
    """
    return frozenset(
        r[1] for r in db._conn.execute("PRAGMA table_info(sessions)").fetchall()
    )


def _insert(db, session_id, *, idle_days, **overrides):
    """Insert an open row that has done real work (the leaking shape)."""
    row = {
        "id": session_id,
        "source": "api_server",
        "user_id": "user-1",
        "session_key": None,
        "started_at": time.time() - idle_days * DAY,
        "last_activity_at": time.time() - idle_days * DAY,
        "message_count": 2,
        "archived": 0,
        "pinned": 0,
    }
    row.update(overrides)
    # Column names come from an explicit allow-list rather than straight from
    # the dict keys. Values were always bound as parameters, but interpolating
    # caller-supplied **overrides keys into the statement text meant a typo'd
    # kwarg produced a confusing SQL syntax error instead of a clear one — and
    # it is a bad shape to copy into a helper that later takes real input.
    unknown = set(row) - _sessions_columns(db)
    if unknown:
        raise ValueError(f"not a sessions column: {sorted(unknown)}")
    cols = ", ".join(sorted(row))
    placeholders = ", ".join("?" for _ in row)
    db._conn.execute(
        f"INSERT INTO sessions ({cols}) VALUES ({placeholders})",
        [row[c] for c in sorted(row)],
    )
    db._conn.commit()
    return session_id


class TestSelector:
    def test_claims_stale_unkeyed_open_row(self, db):
        """The api_server shape: NULL key, real transcript, never closed."""
        _insert(db, "api-orphan", idle_days=5)
        found = db.list_orphaned_open_sessions(older_than_seconds=DAY)
        assert [r["id"] for r in found] == ["api-orphan"]

    def test_claims_stale_keyed_row_too(self, db):
        """A key alone is no protection — it may be evicted or from a dead
        process. The caller decides by checking the live index."""
        _insert(db, "matrix-orphan", idle_days=69, source="matrix",
                session_key="agent:main:matrix:group:!room:$evt")
        found = db.list_orphaned_open_sessions(older_than_seconds=DAY)
        assert [r["id"] for r in found] == ["matrix-orphan"]

    def test_refuses_row_inside_the_idle_floor(self, db):
        """A merely-slow session must never be closed underneath its turn."""
        _insert(db, "recent", idle_days=0.1)
        assert db.list_orphaned_open_sessions(older_than_seconds=DAY) == []

    def test_refuses_already_ended_row(self, db):
        _insert(db, "ended", idle_days=30, ended_at=time.time() - 20 * DAY,
                end_reason="cli_close")
        assert db.list_orphaned_open_sessions(older_than_seconds=DAY) == []

    @pytest.mark.parametrize("field", ["pinned", "archived"])
    def test_refuses_explicit_user_intent(self, db, field):
        """pinned/archived are explicit "keep this" — never auto-close."""
        _insert(db, f"kept-{field}", idle_days=30, **{field: 1})
        assert db.list_orphaned_open_sessions(older_than_seconds=DAY) == []

    def test_falls_back_to_started_at_when_never_active(self, db):
        """last_activity_at is NULL on rows that never recorded activity;
        age must still be judged, not skipped."""
        _insert(db, "no-activity", idle_days=10, last_activity_at=None)
        found = db.list_orphaned_open_sessions(older_than_seconds=DAY)
        assert [r["id"] for r in found] == ["no-activity"]

    def test_orders_oldest_first(self, db):
        _insert(db, "newer", idle_days=3)
        _insert(db, "older", idle_days=40)
        found = db.list_orphaned_open_sessions(older_than_seconds=DAY)
        assert [r["id"] for r in found] == ["older", "newer"]


class TestEnding:
    def test_end_session_closes_the_orphan_without_deleting_it(self, db):
        """The remediation is ENDING, never deletion: these rows hold real
        transcripts."""
        _insert(db, "api-orphan", idle_days=5)
        db.end_session("api-orphan", "orphaned_expiry")

        assert db.list_orphaned_open_sessions(older_than_seconds=DAY) == []
        row = db._conn.execute(
            "SELECT ended_at, end_reason, message_count FROM sessions WHERE id = ?",
            ("api-orphan",),
        ).fetchone()
        assert row["ended_at"] is not None
        assert row["end_reason"] == "orphaned_expiry"
        assert row["message_count"] == 2  # transcript untouched

    def test_end_session_is_idempotent_first_reason_wins(self, db):
        """A concurrent close from the in-memory path must not be clobbered."""
        _insert(db, "raced", idle_days=5)
        db.end_session("raced", "session_reset")
        db.end_session("raced", "orphaned_expiry")
        row = db._conn.execute(
            "SELECT end_reason FROM sessions WHERE id = ?", ("raced",)
        ).fetchone()
        assert row["end_reason"] == "session_reset"
