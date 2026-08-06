"""Regression: the desktop cross-profile sidebar opens every profile's
state.db read-only, and read-only opens skip schema reconciliation. A
dormant profile's DB can therefore be older than the running binary (e.g.
missing last_activity_at / last_read_at) while the sidebar queries assume
the current schema. list_sessions_rich must degrade on such DBs instead of
raising — the sidebar's per-profile try/except swallows the error and
silently drops the whole profile, which reads as "sessions are gone" in
the UI.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL


def _make_stale_db(tmp_path: Path) -> Path:
    """A sessions DB with only the pre-heartbeat columns."""
    db_path = tmp_path / "stale.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            model TEXT,
            title TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            archived INTEGER DEFAULT 0,
            parent_session_id TEXT,
            model_config TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            role TEXT,
            content TEXT,
            timestamp REAL
        )
        """
    )
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, source, model, title, started_at, ended_at, "
        "message_count, archived) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sess-1", "tui", "gpt-5.6-sol", "older session", now - 3600, now - 3500, 2, 0),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        ("sess-1", "user", "hello from the past", now - 3600),
    )
    conn.commit()
    conn.close()
    return db_path


class TestStaleSchemaReadOnly:
    def test_compact_rows_list_degrades_on_stale_db(self, tmp_path):
        db_path = _make_stale_db(tmp_path)
        db = SessionDB(db_path=db_path, read_only=True)
        try:
            rows = db.list_sessions_rich(
                limit=10,
                offset=0,
                min_message_count=1,
                compact_rows=True,
                order_by_last_active=True,
                include_pinned=True,
            )
        finally:
            db.close()
        assert [r["id"] for r in rows] == ["sess-1"]
        # last_active degrades to MAX(messages.timestamp) + started_at fallback.
        assert rows[0]["last_active"] is not None
        # Stale columns the binary wants are absent from the row, not errors.
        assert rows[0].get("last_activity_at") is None
        assert rows[0].get("last_read_at") is None

    def test_declared_projection_still_applies_when_schema_current(self, tmp_path):
        # Fresh schema: probe must see every column the mixin declares.
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            live = db._live_session_columns()
        finally:
            db.close()
        assert live is not None
        declared = set(db._parse_schema_columns(SCHEMA_SQL)["sessions"].keys())
        assert declared <= live

    def test_compact_cols_live_intersection(self, tmp_path):
        db = SessionDB(db_path=tmp_path / "fresh.db")
        try:
            full = db._compact_session_cols()
            degraded = db._compact_session_cols_live({"id", "source"})
            assert "s.last_activity_at" not in degraded
            assert "s.id" in degraded and "s.source" in degraded
            assert len(degraded.split(",")) < len(full.split(","))
        finally:
            db.close()
