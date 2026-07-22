import sqlite3
from pathlib import Path

import pytest

import hermes_state


EXPECTED_BRIDGE_TABLES = {
    "external_sessions",
    "external_message_map",
    "session_links",
    "session_mirror_jobs",
    "session_sidebar_jobs",
    "session_sidebar_terminal_resolutions",
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
    "idx_session_sidebar_jobs_state_next_attempt_at": (
        "state",
        "next_attempt_at",
    ),
    "idx_session_sidebar_jobs_source_session_id": ("source_session_id",),
    "idx_session_sidebar_jobs_lease_digest": ("lease_digest",),
    "idx_session_sidebar_jobs_completion_digest": ("completion_digest",),
    "idx_session_sidebar_jobs_visible_at": ("state", "visible_at", "id"),
}

EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL = {
    "idx_session_sidebar_jobs_lease_digest": (
        "CREATE INDEX idx_session_sidebar_jobs_lease_digest "
        "ON session_sidebar_jobs(lease_digest) WHERE lease_digest IS NOT NULL"
    ),
    "idx_session_sidebar_jobs_completion_digest": (
        "CREATE INDEX idx_session_sidebar_jobs_completion_digest "
        "ON session_sidebar_jobs(completion_digest) "
        "WHERE completion_digest IS NOT NULL"
    ),
    "idx_session_sidebar_jobs_visible_at": (
        "CREATE INDEX idx_session_sidebar_jobs_visible_at "
        "ON session_sidebar_jobs(state, visible_at DESC, id DESC) "
        "WHERE visible_at IS NOT NULL"
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
    "session_sidebar_jobs": {
        ("source_session_id", "sessions", "id", "NO ACTION"),
    },
    "session_sidebar_terminal_resolutions": {
        ("job_id", "session_sidebar_jobs", "id", "RESTRICT"),
    },
    "session_context_packs": {
        ("source_session_id", "sessions", "id", "NO ACTION"),
        ("target_session_id", "sessions", "id", "NO ACTION"),
    },
    "session_bridge_state": set(),
}

V20_MIRROR_JOB_COLUMNS = (
    "id",
    "idempotency_key",
    "source_session_id",
    "target_provider",
    "state",
    "attempts",
    "next_attempt_at",
    "target_native_id",
    "error_code",
    "error_detail",
    "created_at",
    "updated_at",
)
V20_MIRROR_JOB_ROW = (
    "existing-mirror-job",
    "existing-idempotency-key",
    "existing-session",
    "codex",
    "retry",
    3,
    1010.25,
    "existing-native-target",
    "existing-error-code",
    "existing error detail",
    1002.5,
    1009.75,
)


def _prepare_v20_database(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(hermes_state.SCHEMA_SQL)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS session_mirror_jobs (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                source_session_id TEXT NOT NULL REFERENCES sessions(id),
                target_provider TEXT NOT NULL CHECK (target_provider IN ('claude', 'codex')),
                state TEXT NOT NULL CHECK (
                    state IN ('queued', 'running', 'retry', 'succeeded', 'manual_failure')
                ),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                target_native_id TEXT,
                error_code TEXT,
                error_detail TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_session_mirror_jobs_state_next_attempt_at
                ON session_mirror_jobs(state, next_attempt_at);
            """
        )
        conn.execute("INSERT INTO schema_version (version) VALUES (20)")
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
        placeholders = ", ".join("?" for _ in V20_MIRROR_JOB_COLUMNS)
        conn.execute(
            "INSERT INTO session_mirror_jobs "
            f"({', '.join(V20_MIRROR_JOB_COLUMNS)}) VALUES ({placeholders})",
            V20_MIRROR_JOB_ROW,
        )
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


def _insert_sidebar_job(
    conn: sqlite3.Connection,
    *,
    job_id: str = "sidebar-job-1",
    state: str = "sidebar_pending",
    attempts: int = 0,
    lease_digest: str | None = None,
    lease_expires_at: float | None = None,
    completion_digest: str | None = None,
    codex_thread_id: str | None = None,
    visible_at: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO session_sidebar_jobs ("
        "id, idempotency_key, source_session_id, bridge_id, state, attempts, "
        "next_attempt_at, lease_digest, lease_expires_at, completion_digest, "
        "codex_thread_id, eligible_at, created_at, updated_at, visible_at"
        ") VALUES (?, ?, 'source', ?, ?, ?, 2, ?, ?, ?, ?, 1, 1, 1, ?)",
        (
            job_id,
            f"idempotency-{job_id}",
            f"bridge-{job_id}",
            state,
            attempts,
            lease_digest,
            lease_expires_at,
            completion_digest,
            codex_thread_id,
            visible_at,
        ),
    )


def _read_v20_mirror_job(conn: sqlite3.Connection) -> tuple:
    row = conn.execute(
        "SELECT "
        + ", ".join(V20_MIRROR_JOB_COLUMNS)
        + " FROM session_mirror_jobs WHERE id = ?",
        (V20_MIRROR_JOB_ROW[0],),
    ).fetchone()
    assert row is not None
    return tuple(row)


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


def test_fresh_database_creates_bridge_tables_indexes_and_current_schema(tmp_path):
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
        assert version == hermes_state.SCHEMA_VERSION

        for index_name, expected_columns in EXPECTED_BRIDGE_INDEXES.items():
            rows = db._conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            assert tuple(row[2] for row in rows) == expected_columns
        for index_name, expected_sql in EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL.items():
            row = db._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            assert row is not None
            assert " ".join(row[0].split()) == expected_sql
    finally:
        db.close()


def test_v20_database_upgrades_without_changing_existing_rows(tmp_path):
    db_path = tmp_path / "v20.db"
    message_id = _prepare_v20_database(db_path)

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

        assert version == hermes_state.SCHEMA_VERSION
        assert tuple(session) == ("existing-session", "cli", 1000.0)
        assert tuple(message) == (
            message_id,
            "existing-session",
            "user",
            "preserve me",
        )
        assert _read_v20_mirror_job(db._conn) == V20_MIRROR_JOB_ROW
    finally:
        db.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        assert _read_v20_mirror_job(reopened._conn) == V20_MIRROR_JOB_ROW
    finally:
        reopened.close()


def test_reopening_upgraded_database_is_idempotent(tmp_path):
    db_path = tmp_path / "reopen.db"
    _prepare_v20_database(db_path)

    first_open = hermes_state.SessionDB(db_path)
    first_open.close()
    first_objects = _bridge_objects(db_path)

    second_open = hermes_state.SessionDB(db_path)
    second_open.close()

    conn = sqlite3.connect(db_path)
    try:
        versions = conn.execute("SELECT version FROM schema_version").fetchall()
        assert versions == [(hermes_state.SCHEMA_VERSION,)]
        assert _bridge_objects(db_path) == first_objects
        assert len(first_objects) == len(EXPECTED_BRIDGE_TABLES) + len(
            EXPECTED_BRIDGE_INDEXES
        )
    finally:
        conn.close()


def test_v26_database_replaces_abort_trigger_for_exact_max_attempt_absence(tmp_path):
    db_path = tmp_path / "v26-characterization-abort-trigger.db"
    current = hermes_state.SessionDB(db_path)
    current.close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TRIGGER trg_claude_characterization_abort_order")
        conn.execute(
            """CREATE TRIGGER trg_claude_characterization_abort_order
               BEFORE INSERT ON session_claude_visibility_characterization_events
               WHEN NEW.event_kind = 'launch_aborted' AND NOT EXISTS (
                   SELECT 1 FROM session_claude_visibility_jobs AS job
                   WHERE job.id = NEW.job_id AND job.state = 'claude_retry'
               )
               BEGIN
                   SELECT RAISE(
                       ABORT,
                       'Claude characterization abort is not anchored'
                   );
               END"""
        )
        conn.execute(
            "DELETE FROM session_bridge_migrations "
            "WHERE migration_name = 'claude_characterization_abort_max_attempts_v27'"
        )
        conn.execute("UPDATE schema_version SET version = 26")
        conn.commit()
    finally:
        conn.close()

    upgraded = hermes_state.SessionDB(db_path)
    try:
        trigger_sql = upgraded._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("trg_claude_characterization_abort_order",),
        ).fetchone()[0]
        version = upgraded._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0]
        migration_count = upgraded._conn.execute(
            "SELECT COUNT(*) FROM session_bridge_migrations WHERE migration_name = ?",
            ("claude_characterization_abort_max_attempts_v27",),
        ).fetchone()[0]
    finally:
        upgraded.close()

    normalized = " ".join(trigger_sql.split())
    assert "job.state = 'claude_failed'" in normalized
    assert "job.error_code = 'max_attempts_exhausted'" in normalized
    assert version == hermes_state.SCHEMA_VERSION
    assert migration_count == 1


def test_reopening_current_database_repairs_missing_sidebar_indexes_without_data_loss(
    tmp_path,
):
    db_path = tmp_path / "current-missing-sidebar-digest-indexes.db"
    db = hermes_state.SessionDB(db_path)
    try:
        _seed_sessions(db._conn, "source")
        _insert_sidebar_job(
            db._conn,
            job_id="leased-job",
            state="sidebar_leased",
            lease_digest="lease-digest",
            lease_expires_at=500.0,
        )
        _insert_sidebar_job(
            db._conn,
            job_id="visible-job",
            state="sidebar_visible",
            completion_digest="completion-digest",
            codex_thread_id="codex-thread",
            visible_at=200.0,
        )
        db._conn.commit()
    finally:
        db.close()

    conn = sqlite3.connect(db_path)
    try:
        for index_name in EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL:
            conn.execute(f'DROP INDEX "{index_name}"')
        before = conn.execute(
            """SELECT id, state, lease_digest, completion_digest,
                      codex_thread_id, visible_at
               FROM session_sidebar_jobs ORDER BY id"""
        ).fetchall()
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [
            (hermes_state.SCHEMA_VERSION,)
        ]
        placeholders = ",".join("?" for _ in EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL)
        missing = conn.execute(
            f"SELECT name FROM sqlite_master "
            f"WHERE type = 'index' AND name IN ({placeholders})",
            tuple(EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL),
        ).fetchall()
        assert missing == []
        conn.commit()
    finally:
        conn.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        after = reopened._conn.execute(
            """SELECT id, state, lease_digest, completion_digest,
                      codex_thread_id, visible_at
               FROM session_sidebar_jobs ORDER BY id"""
        ).fetchall()
        assert [tuple(row) for row in after] == before
        versions = reopened._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchall()
        assert [tuple(row) for row in versions] == [(hermes_state.SCHEMA_VERSION,)]
        for index_name, expected_sql in EXPECTED_SIDEBAR_PARTIAL_INDEX_SQL.items():
            row = reopened._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            assert row is not None
            assert " ".join(row[0].split()) == expected_sql
    finally:
        reopened.close()


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
    ("column", "duplicate_value"),
    [
        ("idempotency_key", "same-key"),
        ("bridge_id", "same-bridge"),
        ("codex_thread_id", "same-thread"),
    ],
)
def test_sidebar_job_unique_fields_are_rejected(tmp_path, column, duplicate_value):
    db = hermes_state.SessionDB(tmp_path / f"sidebar-{column}-unique.db")
    try:
        conn = db._conn
        _seed_sessions(conn, "source")
        conn.execute(
            "INSERT INTO session_sidebar_jobs "
            "(id, idempotency_key, source_session_id, bridge_id, state, "
            "next_attempt_at, codex_thread_id, eligible_at, created_at, updated_at) "
            "VALUES ('job-1', 'same-key', 'source', 'same-bridge', "
            "'sidebar_pending', 1, 'same-thread', 1, 1, 1)"
        )

        values = {
            "idempotency_key": "different-key",
            "bridge_id": "different-bridge",
            "codex_thread_id": "different-thread",
        }
        values[column] = duplicate_value
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO session_sidebar_jobs "
                "(id, idempotency_key, source_session_id, bridge_id, state, "
                "next_attempt_at, codex_thread_id, eligible_at, created_at, updated_at) "
                "VALUES (?, ?, 'source', ?, 'sidebar_pending', 2, ?, 2, 2, 2)",
                (
                    "job-2",
                    values["idempotency_key"],
                    values["bridge_id"],
                    values["codex_thread_id"],
                ),
            )
    finally:
        db.close()


def test_sidebar_job_source_session_foreign_key_is_enforced(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "sidebar-source-foreign-key.db")
    try:
        with pytest.raises(
            sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"
        ):
            db._conn.execute(
                "INSERT INTO session_sidebar_jobs "
                "(id, idempotency_key, source_session_id, bridge_id, state, "
                "next_attempt_at, eligible_at, created_at, updated_at) "
                "VALUES ('job-1', 'idem-1', 'missing', 'bridge-1', "
                "'sidebar_pending', 1, 1, 1, 1)"
            )
    finally:
        db.close()


def test_sidebar_job_rejects_negative_attempts(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "sidebar-negative-attempts.db")
    try:
        _seed_sessions(db._conn, "source")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_sidebar_job(db._conn, attempts=-1)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("lease_digest", "lease_expires_at"),
    [
        (None, None),
        ("lease-digest", None),
        (None, 5.0),
    ],
    ids=["both-missing", "expiry-missing", "digest-missing"],
)
def test_sidebar_leased_requires_both_lease_fields(
    tmp_path, lease_digest, lease_expires_at
):
    db = hermes_state.SessionDB(tmp_path / "sidebar-leased-fields.db")
    try:
        _seed_sessions(db._conn, "source")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_sidebar_job(
                db._conn,
                state="sidebar_leased",
                lease_digest=lease_digest,
                lease_expires_at=lease_expires_at,
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "state",
    [
        "sidebar_pending",
        "sidebar_visible",
        "sidebar_retry",
        "sidebar_failed",
    ],
)
@pytest.mark.parametrize(
    ("lease_digest", "lease_expires_at"),
    [
        ("lease-digest", None),
        (None, 5.0),
        ("lease-digest", 5.0),
    ],
    ids=["digest-present", "expiry-present", "both-present"],
)
def test_non_leased_sidebar_states_reject_active_lease_fields(
    tmp_path, state, lease_digest, lease_expires_at
):
    db = hermes_state.SessionDB(tmp_path / "sidebar-non-leased-fields.db")
    try:
        _seed_sessions(db._conn, "source")
        visible_fields = (
            {
                "completion_digest": "completion-digest",
                "codex_thread_id": "thread-1",
                "visible_at": 6.0,
            }
            if state == "sidebar_visible"
            else {}
        )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_sidebar_job(
                db._conn,
                state=state,
                lease_digest=lease_digest,
                lease_expires_at=lease_expires_at,
                **visible_fields,
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "missing_field",
    ["codex_thread_id", "visible_at", "completion_digest"],
)
def test_sidebar_visible_requires_every_completion_field(tmp_path, missing_field):
    db = hermes_state.SessionDB(tmp_path / "sidebar-visible-fields.db")
    try:
        _seed_sessions(db._conn, "source")
        visible_fields = {
            "completion_digest": "completion-digest",
            "codex_thread_id": "thread-1",
            "visible_at": 6.0,
        }
        visible_fields[missing_field] = None
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_sidebar_job(
                db._conn,
                state="sidebar_visible",
                **visible_fields,
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    ("state", "fields"),
    [
        ("sidebar_pending", {}),
        (
            "sidebar_leased",
            {"lease_digest": "lease-digest", "lease_expires_at": 5.0},
        ),
        (
            "sidebar_visible",
            {
                "completion_digest": "completion-digest",
                "codex_thread_id": "thread-1",
                "visible_at": 6.0,
            },
        ),
        ("sidebar_retry", {}),
        ("sidebar_failed", {}),
    ],
)
def test_valid_sidebar_job_state_shapes_are_insertable(tmp_path, state, fields):
    db = hermes_state.SessionDB(tmp_path / "sidebar-valid-shapes.db")
    try:
        _seed_sessions(db._conn, "source")
        _insert_sidebar_job(db._conn, state=state, **fields)

        assert (
            db._conn.execute("SELECT state FROM session_sidebar_jobs").fetchone()[0]
            == state
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
        (
            "INSERT INTO session_sidebar_jobs "
            "(id, idempotency_key, source_session_id, bridge_id, state, "
            "next_attempt_at, eligible_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, 1, 1, 1)",
            ("job-1", "idem-1", "source", "bridge-1", "invalid-state"),
        ),
    ],
    ids=[
        "external-provider",
        "origin-kind",
        "relation",
        "target-provider",
        "job-state",
        "sidebar-job-state",
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


def test_bridge_schema_failure_rolls_back_ddl_and_keeps_v20(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    message_id = _prepare_v20_database(db_path)
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

        assert bridge_tables == {"session_mirror_jobs"}
        assert version == 20
        assert session == ("existing-session",)
        assert message == ("preserve me",)
        assert _read_v20_mirror_job(conn) == V20_MIRROR_JOB_ROW
    finally:
        conn.close()
