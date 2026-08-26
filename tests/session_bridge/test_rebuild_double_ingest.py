"""Regression tests for the 2026-08-06..08-10 root-db double-ingest.

A rebuild used to delete prior messages by joining ``external_message_map``.
That map cascades from ``external_sessions``, so a session whose map rows had
been lost kept its existing copy while the rebuild's insert appended a second
one -- 287,351 duplicate rows across 1,551 sessions in the live root state.db,
each stored transcript held exactly twice.

Two independent guards are pinned here: the rebuild now deletes by
``session_id`` (cause), and ``idx_messages_native_event_key`` refuses a second
row for an event a session already stores (constraint).
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_state import SessionDB
from session_bridge.models import (
    ProjectedMessage,
    Provider,
    SessionProjection,
)
from session_bridge.store import SessionBridgeStore


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


def _message(event_id: str, content: str, *, ordinal: int = 0) -> ProjectedMessage:
    return ProjectedMessage(
        native_event_id=event_id,
        ordinal=ordinal,
        role="user",
        content=content,
        timestamp=10.0 + len(event_id),
    )


def _projection(*messages: ProjectedMessage, cursor: str = "cursor-1",
                native_hash: str = "hash-1") -> SessionProjection:
    return SessionProjection(
        provider=Provider.CLAUDE,
        native_id="native-1",
        title="claude session",
        cwd="C:/workspace/project",
        started_at=10.0,
        last_active=20.0,
        messages=tuple(messages),
        native_path="C:/claude/native-1.jsonl",
        native_status="active",
        native_cursor=cursor,
        native_hash=native_hash,
        parser_version=3,
    )


def _rows(db, session_id: str) -> list[str]:
    with db._lock:
        return [
            r[0]
            for r in db._conn.execute(
                "SELECT content FROM messages WHERE session_id=? ORDER BY id",
                (session_id,),
            )
        ]


def test_rebuild_removes_copies_the_map_no_longer_points_at(db):
    """The exact live failure: map rows gone, rebuild must not double the rows."""
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    result = store.upsert_projection(_projection(_message("e1", "one"),
                                                 _message("e2", "two")))
    session_id = result.session_id
    assert _rows(db, session_id) == ["one", "two"]

    # Simulate the lost-map condition the cascade produces.
    with db._lock:
        db._conn.execute(
            "DELETE FROM external_message_map WHERE session_id=?", (session_id,)
        )
        db._conn.commit()

    store.upsert_projection(
        _projection(_message("e1", "one"), _message("e2", "two"),
                    cursor="c2", native_hash="h2"),
        rebuild=True,
    )

    # Pre-fix this returned ["one","two","one","two"].
    assert _rows(db, session_id) == ["one", "two"]


def test_rebuild_with_intact_map_still_replaces_content(db):
    """Control: the ordinary rebuild path must keep replacing, not accumulate."""
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    result = store.upsert_projection(_projection(_message("e1", "old")))
    session_id = result.session_id

    store.upsert_projection(
        _projection(_message("e2", "new"), cursor="c2", native_hash="h2"),
        rebuild=True,
    )

    assert _rows(db, session_id) == ["new"]


def test_ingest_populates_native_event_key(db):
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    result = store.upsert_projection(_projection(_message("e1", "one")))
    with db._lock:
        keys = [
            r[0]
            for r in db._conn.execute(
                "SELECT native_event_key FROM messages WHERE session_id=?",
                (result.session_id,),
            )
        ]
    assert keys == ["e1:0"]


def test_unique_index_refuses_a_second_row_for_the_same_event(db):
    """The storage-layer guard, independent of the rebuild fix."""
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    result = store.upsert_projection(_projection(_message("e1", "one")))

    with db._lock:
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp, "
                "native_event_key) VALUES (?, 'user', 'dupe', 11.0, 'e1:0')",
                (result.session_id,),
            )


def test_null_native_event_key_is_exempt_from_the_index(db):
    """Non-ingested rows must stay insertable in bulk (partial index)."""
    store = SessionBridgeStore(db, clock=lambda: 100.0)
    result = store.upsert_projection(_projection(_message("e1", "one")))

    with db._lock:
        for _ in range(3):
            db._conn.execute(
                "INSERT INTO messages (session_id, role, content, timestamp) "
                "VALUES (?, 'user', 'plain', 12.0)",
                (result.session_id,),
            )
        db._conn.commit()
        n = db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=? "
            "AND native_event_key IS NULL",
            (result.session_id,),
        ).fetchone()[0]
    assert n == 3
