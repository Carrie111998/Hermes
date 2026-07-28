from __future__ import annotations

import sqlite3

from hermes_state import SessionDB


ROLE_SENSITIVE_AGGREGATE = """
    SELECT s.id,
           COUNT(m.id),
           COUNT(CASE WHEN LOWER(m.role) = 'user' THEN 1 END),
           COALESCE(MAX(m.timestamp), 0)
    FROM sessions s
    LEFT JOIN messages m ON m.session_id = s.id
    WHERE s.source IS NOT NULL AND s.source NOT IN ('cron', 'webui')
    GROUP BY s.id
    ORDER BY s.id
"""


def _index_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (name,)
        ).fetchall()
    ]


def test_role_sensitive_message_aggregate_is_covered(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        db.create_session("visible", source="telegram")
        db.append_message("visible", role="user", content="first", timestamp=10)
        db.append_message("visible", role="assistant", content="reply", timestamp=20)
        db.append_message("visible", role="USER", content="second", timestamp=30)
    finally:
        db.close()

    with sqlite3.connect(db_path) as conn:
        assert _index_columns(conn, "idx_messages_session_timestamp_role") == [
            "session_id",
            "timestamp",
            "role",
        ]

        plan = conn.execute(f"EXPLAIN QUERY PLAN {ROLE_SENSITIVE_AGGREGATE}").fetchall()
        details = "\n".join(str(row[3]) for row in plan)
        assert "USING COVERING INDEX idx_messages_session_timestamp_role" in details, (
            details
        )

        assert conn.execute(ROLE_SENSITIVE_AGGREGATE).fetchall() == [
            ("visible", 3, 2, 30.0)
        ]


def test_existing_database_gets_role_covering_index_on_open(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_messages_session_timestamp_role")
        conn.commit()
        assert _index_columns(conn, "idx_messages_session_timestamp_role") == []

    reopened = SessionDB(db_path=db_path)
    reopened.close()

    with sqlite3.connect(db_path) as conn:
        assert _index_columns(conn, "idx_messages_session_timestamp_role") == [
            "session_id",
            "timestamp",
            "role",
        ]
