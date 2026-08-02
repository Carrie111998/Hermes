"""Regression tests for persisted logical conversation identity."""

from __future__ import annotations

import json
import sqlite3

from hermes_state import SessionDB
from hermes_state_common import SCHEMA_SQL, SCHEMA_VERSION


def test_standalone_session_uses_its_own_id(tmp_path):
    db = SessionDB(tmp_path / "standalone.db")
    try:
        db.create_session("standalone", source="cli")

        row = db.get_session("standalone")
        assert row is not None
        assert row["conversation_id"] == "standalone"
    finally:
        db.close()


def test_compression_continuation_inherits_parent_identity(tmp_path):
    db = SessionDB(tmp_path / "compression.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")

        db.create_session(
            "tip",
            source="cli",
            parent_session_id="root",
        )

        assert db.get_session("root")["conversation_id"] == "root"
        assert db.get_session("tip")["conversation_id"] == "root"
    finally:
        db.close()


def test_branch_and_delegate_children_keep_their_own_identity(tmp_path):
    db = SessionDB(tmp_path / "isolated-children.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")

        db.create_session(
            "branch",
            source="cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )
        db.create_session(
            "delegate",
            source="cli",
            parent_session_id="root",
            model_config={"_delegate_from": "root"},
        )

        assert db.get_session("branch")["conversation_id"] == "branch"
        assert db.get_session("delegate")["conversation_id"] == "delegate"
    finally:
        db.close()


def test_tool_child_keeps_its_own_identity(tmp_path):
    db = SessionDB(tmp_path / "tool-child.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")

        db.create_session(
            "tool-child",
            source="tool",
            parent_session_id="root",
        )

        assert db.get_session("tool-child")["conversation_id"] == "tool-child"
    finally:
        db.close()


class _NoFtsExistingTableCursor(sqlite3.Cursor):
    """Simulate an existing FTS database opened without FTS5 support."""

    def execute(self, sql, parameters=()):
        probe = sql.strip()
        if "USING fts5" in probe:
            raise sqlite3.OperationalError("no such module: fts5")
        if probe in (
            "SELECT * FROM messages_fts LIMIT 0",
            "SELECT * FROM messages_fts_trigram LIMIT 0",
        ):
            raise sqlite3.OperationalError("no such module: fts5")
        return super().execute(sql, parameters)

    def executescript(self, sql_script):
        if "USING fts5" in sql_script:
            raise sqlite3.OperationalError("no such module: fts5")
        return super().executescript(sql_script)


class _NoFtsExistingTableConnection(sqlite3.Connection):
    def cursor(self, factory=None):
        return super().cursor(
            factory or _NoFtsExistingTableCursor
        )


def _create_v23_database(db_path):
    legacy_schema = SCHEMA_SQL.replace(
        "    conversation_id TEXT,\n",
        "",
        1,
    )
    if legacy_schema == SCHEMA_SQL:
        raise AssertionError(
            "conversation_id was not removed from the legacy schema"
        )

    conn = sqlite3.connect(db_path)
    conn.executescript(legacy_schema)
    conn.execute(
        "INSERT INTO schema_version (version) VALUES (23)"
    )
    return conn


def _insert_legacy_session(
    conn,
    session_id,
    *,
    source="cli",
    parent_session_id=None,
    end_reason=None,
    model_config=None,
):
    conn.execute(
        """
        INSERT INTO sessions (
            id,
            source,
            parent_session_id,
            end_reason,
            model_config,
            started_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            source,
            parent_session_id,
            end_reason,
            (
                json.dumps(model_config)
                if model_config is not None
                else None
            ),
            1.0,
        ),
    )


def test_v23_migration_backfills_conservative_conversation_ids(
    tmp_path,
):
    db_path = tmp_path / "legacy-v23.db"
    conn = _create_v23_database(db_path)

    _insert_legacy_session(conn, "standalone")

    _insert_legacy_session(
        conn,
        "root",
        end_reason="compression",
    )
    _insert_legacy_session(
        conn,
        "mid",
        parent_session_id="root",
        end_reason="compression",
    )
    _insert_legacy_session(
        conn,
        "tip",
        parent_session_id="mid",
    )

    _insert_legacy_session(
        conn,
        "branch",
        parent_session_id="root",
        model_config={"_branched_from": "root"},
    )
    _insert_legacy_session(
        conn,
        "delegate",
        parent_session_id="root",
        model_config={"_delegate_from": "root"},
    )
    _insert_legacy_session(
        conn,
        "tool-child",
        source="tool",
        parent_session_id="root",
    )

    _insert_legacy_session(
        conn,
        "ambiguous-parent",
        end_reason="done",
    )
    _insert_legacy_session(
        conn,
        "ambiguous-child",
        parent_session_id="ambiguous-parent",
    )

    _insert_legacy_session(
        conn,
        "orphan",
        parent_session_id="missing-parent",
    )

    _insert_legacy_session(
        conn,
        "cycle-a",
        parent_session_id="cycle-b",
        end_reason="compression",
    )
    _insert_legacy_session(
        conn,
        "cycle-b",
        parent_session_id="cycle-a",
        end_reason="compression",
    )

    conn.commit()
    conn.close()

    db = SessionDB(db_path)
    try:
        expected = {
            "standalone": "standalone",
            "root": "root",
            "mid": "root",
            "tip": "root",
            "branch": "branch",
            "delegate": "delegate",
            "tool-child": "tool-child",
            "ambiguous-parent": "ambiguous-parent",
            "ambiguous-child": "ambiguous-child",
            "orphan": "orphan",
            "cycle-a": "cycle-a",
            "cycle-b": "cycle-b",
        }

        actual = {
            session_id: db.get_session(session_id)["conversation_id"]
            for session_id in expected
        }

        assert actual == expected

        stored_version = db._conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()[0]
        assert stored_version == SCHEMA_VERSION

        index = db._conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'index'
              AND name = 'idx_sessions_conversation_id'
            """
        ).fetchone()
        assert index is not None
    finally:
        db.close()

    reopened = SessionDB(db_path)
    try:
        reopened_values = {
            session_id: reopened.get_session(session_id)[
                "conversation_id"
            ]
            for session_id in expected
        }
        assert reopened_values == expected
    finally:
        reopened.close()


def test_v23_migration_backfills_without_fts5(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "legacy-v23-no-fts.db"
    conn = _create_v23_database(db_path)

    _insert_legacy_session(
        conn,
        "no-fts-root",
        end_reason="compression",
    )
    _insert_legacy_session(
        conn,
        "no-fts-tip",
        parent_session_id="no-fts-root",
    )

    conn.commit()
    conn.close()

    real_connect = sqlite3.connect

    def connect_without_fts(*args, **kwargs):
        kwargs["factory"] = _NoFtsExistingTableConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(
        sqlite3,
        "connect",
        connect_without_fts,
    )

    expected = {
        "no-fts-root": "no-fts-root",
        "no-fts-tip": "no-fts-root",
    }

    # Open twice to verify that the degraded-runtime migration is
    # idempotent even though schema_version deliberately remains at 23.
    for _ in range(2):
        db = SessionDB(db_path)
        try:
            actual = {
                session_id: db.get_session(session_id)[
                    "conversation_id"
                ]
                for session_id in expected
            }
            assert actual == expected

            stored_version = db._conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()[0]
            assert stored_version == 23

            index = db._conn.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'index'
                  AND name = 'idx_sessions_conversation_id'
                """
            ).fetchone()
            assert index is not None
        finally:
            db.close()


def test_export_import_preserves_conversation_identity(tmp_path):
    source = SessionDB(tmp_path / "export-source.db")
    try:
        source.create_session("export-root", source="cli")
        source.end_session("export-root", "compression")
        source.create_session(
            "export-tip",
            source="cli",
            parent_session_id="export-root",
        )

        exported = [
            source.export_session("export-root"),
            source.export_session("export-tip"),
        ]

        assert exported[0]["conversation_id"] == "export-root"
        assert exported[1]["conversation_id"] == "export-root"
    finally:
        source.close()

    target = SessionDB(tmp_path / "export-target.db")
    try:
        result = target.import_sessions(exported)

        assert result["ok"] is True
        assert result["imported"] == 2
        assert target.get_session("export-root")[
            "conversation_id"
        ] == "export-root"
        assert target.get_session("export-tip")[
            "conversation_id"
        ] == "export-root"
    finally:
        target.close()


def test_legacy_import_backfills_identity_after_parent_restoration(
    tmp_path,
):
    target = SessionDB(tmp_path / "legacy-import.db")
    try:
        result = target.import_sessions(
            [
                {
                    "id": "legacy-root",
                    "source": "cli",
                    "end_reason": "compression",
                    "messages": [],
                },
                {
                    "id": "legacy-tip",
                    "source": "cli",
                    "parent_session_id": "legacy-root",
                    "messages": [],
                },
            ]
        )

        assert result["ok"] is True
        assert result["imported"] == 2
        assert target.get_session("legacy-root")[
            "conversation_id"
        ] == "legacy-root"
        assert target.get_session("legacy-tip")[
            "conversation_id"
        ] == "legacy-root"
    finally:
        target.close()


def test_existing_conversation_identity_is_immutable_on_upsert(
    tmp_path,
):
    db = SessionDB(tmp_path / "immutable-upsert.db")
    try:
        db.create_session("compression-parent", source="cli")
        db.end_session("compression-parent", "compression")

        db.create_session("stable-session", source="cli")
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET conversation_id = ? WHERE id = ?",
                ("preserved-conversation", "stable-session"),
            )
        )

        # A later idempotent creation computes a different candidate from the
        # compression parent, but must not replace the stored logical identity.
        db.create_session(
            "stable-session",
            source="cli",
            parent_session_id="compression-parent",
        )

        row = db.get_session("stable-session")
        assert row["conversation_id"] == "preserved-conversation"
    finally:
        db.close()
