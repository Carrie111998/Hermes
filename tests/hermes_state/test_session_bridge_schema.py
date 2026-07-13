import sqlite3
from pathlib import Path

import pytest

import hermes_state


EXPECTED_BRIDGE_TABLES = {
    "external_sessions",
    "external_message_map",
    "session_links",
    "session_mirror_jobs",
    "session_context_packs",
    "session_bridge_state",
}

EXPECTED_BRIDGE_INDEXES = {
    "idx_external_sessions_last_indexed_at": ("last_indexed_at",),
    "idx_external_sessions_origin_bridge_id": ("origin_bridge_id",),
    "idx_session_links_bridge_id": ("bridge_id",),
    "idx_session_links_from_session_id": ("from_session_id",),
    "idx_session_links_to_session_id": ("to_session_id",),
    "idx_session_mirror_jobs_state_next_attempt_at": (
        "state",
        "next_attempt_at",
    ),
}

EXPECTED_BRIDGE_FOREIGN_KEYS = {
    "external_sessions": {
        ("session_id", "sessions", "id", "CASCADE"),
    },
    "external_message_map": {
        ("session_id", "external_sessions", "session_id", "CASCADE"),
        ("message_id", "messages", "id", "CASCADE"),
    },
    "session_links": {
        ("from_session_id", "sessions", "id", "NO ACTION"),
        ("to_session_id", "sessions", "id", "NO ACTION"),
    },
    "session_mirror_jobs": {
        ("source_session_id", "sessions", "id", "NO ACTION"),
    },
    "session_context_packs": {
        ("source_session_id", "sessions", "id", "NO ACTION"),
        ("target_session_id", "sessions", "id", "NO ACTION"),
    },
    "session_bridge_state": set(),
}


def _prepare_v19_database(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(hermes_state.SCHEMA_SQL)
        conn.execute("INSERT INTO schema_version (version) VALUES (19)")
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("existing-session", "cli", 1000.0),
        )
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("existing-session", "user", "preserve me", 1001.0),
        )
        message_id = cursor.lastrowid
        assert message_id is not None
        conn.commit()
        return message_id
    finally:
        conn.close()


def _seed_sessions(conn: sqlite3.Connection, *session_ids: str) -> None:
    conn.executemany(
        "INSERT INTO sessions (id, source, started_at) VALUES (?, 'cli', 1000.0)",
        [(session_id,) for session_id in session_ids],
    )


def _insert_external_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    provider: str = "claude",
    native_id: str = "native-1",
    origin_kind: str = "native",
) -> None:
    conn.execute(
        "INSERT INTO external_sessions ("
        "session_id, provider, native_id, first_indexed_at, last_indexed_at, "
        "parser_version, origin_kind"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, provider, native_id, 1000.0, 1001.0, 1, origin_kind),
    )


def _bridge_objects(db_path: Path) -> list[tuple[str, str, str]]:
    conn = sqlite3.connect(db_path)
    try:
        names = EXPECTED_BRIDGE_TABLES | set(EXPECTED_BRIDGE_INDEXES)
        placeholders = ",".join("?" for _ in names)
        return conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            f"WHERE name IN ({placeholders}) ORDER BY type, name",
            sorted(names),
        ).fetchall()
    finally:
        conn.close()


def test_fresh_database_creates_bridge_tables_indexes_and_schema_v20(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "fresh.db")
    try:
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = db._conn.execute("SELECT version FROM schema_version").fetchone()[0]

        assert EXPECTED_BRIDGE_TABLES <= tables
        assert version == 20

        for index_name, expected_columns in EXPECTED_BRIDGE_INDEXES.items():
            rows = db._conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            assert tuple(row[2] for row in rows) == expected_columns
    finally:
        db.close()


def test_v19_database_upgrades_without_losing_sessions_or_messages(tmp_path):
    db_path = tmp_path / "v19.db"
    message_id = _prepare_v19_database(db_path)

    db = hermes_state.SessionDB(db_path)
    try:
        version = db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
        session = db._conn.execute(
            "SELECT id, source, started_at FROM sessions WHERE id = ?",
            ("existing-session",),
        ).fetchone()
        message = db._conn.execute(
            "SELECT id, session_id, role, content FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

        assert version == 20
        assert tuple(session) == ("existing-session", "cli", 1000.0)
        assert tuple(message) == (
            message_id,
            "existing-session",
            "user",
            "preserve me",
        )
    finally:
        db.close()


def test_reopening_upgraded_database_is_idempotent(tmp_path):
    db_path = tmp_path / "reopen.db"
    _prepare_v19_database(db_path)

    first_open = hermes_state.SessionDB(db_path)
    first_open.close()
    first_objects = _bridge_objects(db_path)

    second_open = hermes_state.SessionDB(db_path)
    second_open.close()

    conn = sqlite3.connect(db_path)
    try:
        versions = conn.execute("SELECT version FROM schema_version").fetchall()
        assert versions == [(20,)]
        assert _bridge_objects(db_path) == first_objects
        assert len(first_objects) == len(EXPECTED_BRIDGE_TABLES) + len(
            EXPECTED_BRIDGE_INDEXES
        )
    finally:
        conn.close()


def test_bridge_foreign_keys_exist_and_foreign_key_check_is_clean(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "foreign-keys.db")
    try:
        conn = db._conn
        _seed_sessions(conn, "source", "target")
        message_id = conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES ('source', 'user', 'mapped', 1001.0)"
        ).lastrowid
        _insert_external_session(conn, session_id="source")
        conn.execute(
            "INSERT INTO external_message_map "
            "(session_id, native_event_id, ordinal, message_id) "
            "VALUES ('source', 'event-1', 0, ?)",
            (message_id,),
        )
        conn.execute(
            "INSERT INTO session_links "
            "(id, from_session_id, to_session_id, relation, bridge_id, created_at) "
            "VALUES ('link-1', 'source', 'target', 'mirrors', 'bridge-1', 1002.0)"
        )
        conn.execute(
            "INSERT INTO session_mirror_jobs "
            "(id, idempotency_key, source_session_id, target_provider, state, "
            "next_attempt_at, created_at, updated_at) "
            "VALUES ('job-1', 'idem-1', 'source', 'codex', 'queued', "
            "1003.0, 1003.0, 1003.0)"
        )
        conn.execute(
            "INSERT INTO session_context_packs "
            "(id, bridge_id, source_session_id, target_session_id, source_cursor, "
            "source_hash, budget_chars, payload, created_at) "
            "VALUES ('pack-1', 'bridge-1', 'source', 'target', 'cursor-1', "
            "'hash-1', 1000, '{}', 1004.0)"
        )

        for table_name, expected in EXPECTED_BRIDGE_FOREIGN_KEYS.items():
            rows = conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
            actual = {(row[3], row[2], row[4], row[6]) for row in rows}
            assert actual == expected

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        db.close()


def test_duplicate_provider_native_id_is_rejected(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "external-unique.db")
    try:
        conn = db._conn
        _seed_sessions(conn, "source-1", "source-2")
        _insert_external_session(
            conn,
            session_id="source-1",
            provider="claude",
            native_id="same-native-id",
        )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_external_session(
                conn,
                session_id="source-2",
                provider="claude",
                native_id="same-native-id",
            )
    finally:
        db.close()


def test_duplicate_mirror_job_idempotency_key_is_rejected(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "job-unique.db")
    try:
        conn = db._conn
        _seed_sessions(conn, "source")
        conn.execute(
            "INSERT INTO session_mirror_jobs "
            "(id, idempotency_key, source_session_id, target_provider, state, "
            "next_attempt_at, created_at, updated_at) "
            "VALUES ('job-1', 'same-key', 'source', 'claude', 'queued', 1, 1, 1)"
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_mirror_jobs "
                "(id, idempotency_key, source_session_id, target_provider, state, "
                "next_attempt_at, created_at, updated_at) "
                "VALUES ('job-2', 'same-key', 'source', 'codex', 'retry', 2, 2, 2)"
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        (
            "INSERT INTO external_sessions "
            "(session_id, provider, native_id, first_indexed_at, last_indexed_at, "
            "parser_version, origin_kind) VALUES (?, ?, ?, 1, 1, 1, ?)",
            ("source", "invalid-provider", "native-1", "native"),
        ),
        (
            "INSERT INTO external_sessions "
            "(session_id, provider, native_id, first_indexed_at, last_indexed_at, "
            "parser_version, origin_kind) VALUES (?, ?, ?, 1, 1, 1, ?)",
            ("source", "claude", "native-1", "invalid-origin"),
        ),
        (
            "INSERT INTO session_links "
            "(id, from_session_id, to_session_id, relation, bridge_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            ("link-1", "source", "target", "invalid-relation", "bridge-1"),
        ),
        (
            "INSERT INTO session_mirror_jobs "
            "(id, idempotency_key, source_session_id, target_provider, state, "
            "next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, 1)",
            ("job-1", "idem-1", "source", "invalid-provider", "queued"),
        ),
        (
            "INSERT INTO session_mirror_jobs "
            "(id, idempotency_key, source_session_id, target_provider, state, "
            "next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 1, 1)",
            ("job-1", "idem-1", "source", "codex", "invalid-state"),
        ),
    ],
    ids=[
        "external-provider",
        "origin-kind",
        "relation",
        "target-provider",
        "job-state",
    ],
)
def test_bridge_check_constraints_reject_invalid_values(tmp_path, sql, params):
    db = hermes_state.SessionDB(tmp_path / "checks.db")
    try:
        _seed_sessions(db._conn, "source", "target")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            db._conn.execute(sql, params)
    finally:
        db.close()


def test_bridge_schema_failure_rolls_back_ddl_and_keeps_v19(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    message_id = _prepare_v19_database(db_path)
    injected_schema = """
    CREATE TABLE external_sessions (session_id TEXT PRIMARY KEY);
    CREATE TABLE session_bridge_state (key TEXT PRIMARY KEY);
    THIS IS DELIBERATELY INVALID SQL;
    """
    monkeypatch.setattr(
        hermes_state,
        "BRIDGE_SCHEMA_SQL",
        injected_schema,
        raising=False,
    )

    with pytest.raises(sqlite3.OperationalError):
        hermes_state.SessionDB(db_path)

    conn = sqlite3.connect(db_path)
    try:
        bridge_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        } & EXPECTED_BRIDGE_TABLES
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        session = conn.execute(
            "SELECT id FROM sessions WHERE id = 'existing-session'"
        ).fetchone()
        message = conn.execute(
            "SELECT content FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

        assert bridge_tables == set()
        assert version == 19
        assert session == ("existing-session",)
        assert message == ("preserve me",)
    finally:
        conn.close()
