"""Gateway-routing PK healing must preserve cold-archive fences."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_VERSION


_ROUTE_TOMBSTONE_TRIGGERS = {
    "gateway_routing_reject_cold_archive_tombstone_insert",
    "gateway_routing_reject_cold_archive_tombstone_update",
}


_LEGACY_GATEWAY_ROUTING_SQL = """
CREATE TABLE gateway_routing (
    session_key TEXT PRIMARY KEY,
    entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL
)
"""


def _make_legacy_gateway_routing_db(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    db.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE gateway_routing")
        conn.execute(_LEGACY_GATEWAY_ROUTING_SQL)
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    return db_path


def test_legacy_gateway_routing_pk_heal_reinstalls_tombstone_triggers(
    tmp_path,
) -> None:
    db_path = _make_legacy_gateway_routing_db(tmp_path)
    db = SessionDB(db_path=db_path)
    try:
        assert db._conn is not None
        trigger_names = {
            str(row[0])
            for row in db._conn.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'trigger' AND tbl_name = 'gateway_routing'"
            ).fetchall()
        }
        assert _ROUTE_TOMBSTONE_TRIGGERS <= trigger_names

        db._conn.execute(
            "INSERT INTO cold_archive_tombstones "
            "(session_id, terminal_id, source_fingerprint, deleted_at) "
            "VALUES (?, ?, ?, ?)",
            ("purged-id", "purged-id", "f" * 64, 1.0),
        )
        db._conn.commit()

        with sqlite3.connect(db_path) as concurrent:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="gateway route targets a cold-archived session ID",
            ):
                concurrent.execute(
                    "INSERT INTO gateway_routing "
                    "(scope, session_key, entry_json, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "test-scope",
                        "test-key",
                        json.dumps({"session_id": "purged-id"}),
                        2.0,
                    ),
                )
    finally:
        db.close()
