import json
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
    "session_sidebar_reconciliation_proofs",
    "session_sidebar_terminal_resolutions",
    "session_sidebar_v2_attempt_zero_resolutions",
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
    "idx_sidebar_reconciliation_job_created": ("job_id", "created_at"),
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
    "session_sidebar_reconciliation_proofs": {
        ("job_id", "session_sidebar_jobs", "id", "RESTRICT"),
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


def test_sidebar_reconciliation_proof_schema_is_append_only(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "sidebar-reconciliation.db")
    try:
        columns = {
            row[1]
            for row in db._conn.execute(
                'PRAGMA table_info("session_sidebar_reconciliation_proofs")'
            )
        }
        sidebar_job_columns = {
            row[1]
            for row in db._conn.execute(
                'PRAGMA table_info("session_sidebar_jobs")'
            )
        }
        triggers = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = ?",
                ("session_sidebar_reconciliation_proofs",),
            )
        }

        assert columns == {
            "proof_digest",
            "job_id",
            "source_session_id",
            "bridge_id",
            "marker_digest",
            "placement_generation",
            "delivery_generation",
            "reconciliation_generation",
            "completed_at",
            "expires_at",
            "inventory_digest",
            "state",
            "match_count",
            "recovered_thread_id",
            "fixed_reason",
            "created_at",
        }
        assert "reconciliation_proof_digest" in sidebar_job_columns
        assert triggers == {
            "trg_sidebar_reconciliation_proofs_no_update",
            "trg_sidebar_reconciliation_proofs_no_delete",
        }
        # A freshly-opened DB sits at the current version, whatever that is.
        # This was pinned to the literal 31 (the version this table shipped in),
        # which turned every LATER unrelated migration into a false failure --
        # v32 (messages_fts external content) never touches this table, yet it
        # broke this test. The append-only guarantee is carried by the column
        # and trigger assertions above, not by the version number.
        assert (
            db._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == hermes_state.SCHEMA_VERSION
        )
    finally:
        db.close()


def test_sidebar_v2_attempt_zero_resolution_schema_is_append_only(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "sidebar-v2-attempt-zero.db")
    try:
        columns = tuple(
            row[1]
            for row in db._conn.execute(
                'PRAGMA table_info("session_sidebar_v2_attempt_zero_resolutions")'
            )
        )
        foreign_keys = {
            (row[3], row[2], row[4], row[5], row[6])
            for row in db._conn.execute(
                'PRAGMA foreign_key_list("session_sidebar_v2_attempt_zero_resolutions")'
            )
        }
        triggers = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                ("session_sidebar_v2_attempt_zero_resolutions",),
            )
        }
        table_row = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_v2_attempt_zero_resolutions",),
        ).fetchone()
        assert columns == (
            "job_id", "idempotency_key", "source_session_id", "bridge_id",
            "failure_state", "failure_code", "failure_attempts",
            "failure_next_attempt_at", "failure_updated_at", "reservation_reserved_at",
            "reservation_reconciliation_proof_digest",
            "reservation_reconciliation_generation", "proof_completed_at",
            "proof_expires_at", "proof_inventory_digest", "resolution_code",
            "evidence_kind", "evidence_version", "evidence_digest", "resolved_at",
        )
        assert foreign_keys == {
            ("job_id", "session_sidebar_jobs", "id", "RESTRICT", "RESTRICT"),
            (
                "reservation_reconciliation_proof_digest",
                "session_sidebar_reconciliation_proofs", "proof_digest",
                "RESTRICT", "RESTRICT",
            ),
        }
        assert table_row is not None
        table_sql = _normalized_sql(table_row[0])
        assert "failure_attempts INTEGER NOT NULL CHECK (failure_attempts = 0)" in table_sql
        assert "CHECK (resolved_at <= proof_expires_at)" in table_sql
        assert triggers == {
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_replacement",
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_update",
            "trg_session_sidebar_v2_attempt_zero_resolutions_no_delete",
        }
    finally:
        db.close()


def test_sidebar_resolution_ledgers_exclude_v2_attempt_zero_in_both_orders(tmp_path):
    db = hermes_state.SessionDB(tmp_path / "sidebar-v2-mutual-exclusion.db")
    try:
        triggers_by_table = {
            table: {
                row[0]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
                    (table,),
                )
            }
            for table in (
                "session_sidebar_terminal_resolutions",
                "session_sidebar_precreate_resolutions",
                "session_sidebar_unbound_resolutions",
                "session_sidebar_v2_attempt_zero_resolutions",
            )
        }
        replacement = db._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            ("trg_session_sidebar_v2_attempt_zero_resolutions_no_replacement",),
        ).fetchone()
        assert replacement is not None
        replacement_sql = _normalized_sql(replacement[0])
        assert "trg_session_sidebar_terminal_resolutions_no_v2_attempt_zero_overlap" in triggers_by_table["session_sidebar_terminal_resolutions"]
        assert "trg_session_sidebar_precreate_resolutions_no_v2_attempt_zero_overlap" in triggers_by_table["session_sidebar_precreate_resolutions"]
        assert "trg_session_sidebar_unbound_resolutions_no_v2_attempt_zero_overlap" in triggers_by_table["session_sidebar_unbound_resolutions"]
        for existing_table in (
            "session_sidebar_terminal_resolutions",
            "session_sidebar_precreate_resolutions",
            "session_sidebar_unbound_resolutions",
        ):
            assert existing_table in replacement_sql
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


def test_v32_database_repairs_same_named_malformed_v33_trigger_before_advancing(
    tmp_path,
):
    db_path = tmp_path / "v32-malformed-v33-trigger.db"
    initial = hermes_state.SessionDB(db_path)
    initial.close()

    trigger_name = "trg_session_sidebar_v2_attempt_zero_resolutions_no_update"
    malformed_sql = f"""CREATE TRIGGER {trigger_name}
        BEFORE UPDATE ON session_sidebar_v2_attempt_zero_resolutions
        BEGIN SELECT 1; END"""
    raw = sqlite3.connect(db_path)
    try:
        raw.execute(f'DROP TRIGGER "{trigger_name}"')
        raw.execute(malformed_sql)
        raw.execute("UPDATE schema_version SET version = 32")
        raw.commit()
    finally:
        raw.close()

    upgraded = hermes_state.SessionDB(db_path)
    try:
        conn = upgraded._conn
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 33
        repaired_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()[0]
        assert " ".join(repaired_sql.split()) != " ".join(malformed_sql.split())
        from session_bridge.store import SessionBridgeStore

        assert SessionBridgeStore._sidebar_terminal_resolution_ledger_is_valid(conn)
    finally:
        upgraded.close()


def test_v32_v33_trigger_repair_rolls_back_before_schema_marker_on_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "v32-v33-trigger-rollback.db"
    initial = hermes_state.SessionDB(db_path)
    initial.close()

    trigger_name = "trg_session_sidebar_v2_attempt_zero_resolutions_no_update"
    raw = sqlite3.connect(db_path)
    try:
        raw.execute("UPDATE schema_version SET version = 32")
        raw.commit()
    finally:
        raw.close()

    original_repair = hermes_state.SessionDB._repair_v33_sidebar_resolution_triggers

    def fail_after_repair(cursor):
        original_repair(cursor)
        cursor.execute(f'DROP TRIGGER "{trigger_name}"')
        raise RuntimeError("injected v33 repair failure")

    monkeypatch.setattr(
        hermes_state.SessionDB,
        "_repair_v33_sidebar_resolution_triggers",
        staticmethod(fail_after_repair),
    )
    with pytest.raises(RuntimeError, match="injected v33 repair failure"):
        hermes_state.SessionDB(db_path)

    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 32
        assert raw.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone() is not None
    finally:
        raw.close()


def test_v32_database_repairs_malformed_same_named_v33_ledger_before_advancing(
    tmp_path,
):
    db_path = tmp_path / "v32-malformed-v33-ledger.db"
    initial = hermes_state.SessionDB(db_path)
    initial.close()

    raw = sqlite3.connect(db_path)
    try:
        trigger_rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            ("session_sidebar_v2_attempt_zero_resolutions",),
        ).fetchall()
        for (trigger_name,) in trigger_rows:
            raw.execute(f'DROP TRIGGER "{trigger_name}"')
        raw.execute("DROP TABLE session_sidebar_v2_attempt_zero_resolutions")
        raw.execute(
            "CREATE TABLE session_sidebar_v2_attempt_zero_resolutions "
            "(job_id TEXT PRIMARY KEY)"
        )
        raw.execute("UPDATE schema_version SET version = 32")
        raw.commit()
    finally:
        raw.close()

    upgraded = hermes_state.SessionDB(db_path)
    try:
        assert upgraded._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == 33
        from session_bridge.store import SessionBridgeStore

        assert SessionBridgeStore._sidebar_terminal_resolution_ledger_is_valid(
            upgraded._conn
        )
    finally:
        upgraded.close()


def test_v32_database_refuses_to_replace_malformed_v33_ledger_with_evidence(
    tmp_path,
):
    db_path = tmp_path / "v32-malformed-v33-ledger-with-evidence.db"
    initial = hermes_state.SessionDB(db_path)
    initial.close()

    raw = sqlite3.connect(db_path)
    try:
        trigger_rows = raw.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?",
            ("session_sidebar_v2_attempt_zero_resolutions",),
        ).fetchall()
        for (trigger_name,) in trigger_rows:
            raw.execute(f'DROP TRIGGER "{trigger_name}"')
        raw.execute("DROP TABLE session_sidebar_v2_attempt_zero_resolutions")
        raw.execute(
            "CREATE TABLE session_sidebar_v2_attempt_zero_resolutions "
            "(job_id TEXT PRIMARY KEY)"
        )
        raw.execute(
            "INSERT INTO session_sidebar_v2_attempt_zero_resolutions (job_id) "
            "VALUES ('preserve-me')"
        )
        raw.execute("UPDATE schema_version SET version = 32")
        raw.commit()
    finally:
        raw.close()

    with pytest.raises(
        RuntimeError,
        match="malformed v33 sidebar terminal resolution ledger contains evidence",
    ):
        hermes_state.SessionDB(db_path)

    raw = sqlite3.connect(db_path)
    try:
        assert raw.execute("SELECT version FROM schema_version").fetchone()[0] == 32
        assert raw.execute(
            "SELECT job_id FROM session_sidebar_v2_attempt_zero_resolutions"
        ).fetchall() == [("preserve-me",)]
    finally:
        raw.close()


def test_v32_database_adds_v2_attempt_zero_ledger_preserves_rows_and_reopens(
    tmp_path,
):
    db_path = tmp_path / "v32-to-v33.db"
    initial = hermes_state.SessionDB(db_path)
    try:
        conn = initial._conn
        conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("v32-preserved-session", "cli", 1000.0),
        )
        message_id = conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("v32-preserved-session", "user", "preserve through v33", 1001.0),
        ).lastrowid
        conn.commit()
    finally:
        initial.close()

    v33_trigger_names = (
        "trg_session_sidebar_terminal_resolutions_no_v2_attempt_zero_overlap",
        "trg_session_sidebar_precreate_resolutions_no_v2_attempt_zero_overlap",
        "trg_session_sidebar_unbound_resolutions_no_v2_attempt_zero_overlap",
        "trg_session_sidebar_v2_attempt_zero_resolutions_no_replacement",
        "trg_session_sidebar_v2_attempt_zero_resolutions_no_update",
        "trg_session_sidebar_v2_attempt_zero_resolutions_no_delete",
    )
    raw = sqlite3.connect(db_path)
    try:
        # A genuine v32 database never had these v33 objects.  Drop the
        # complete new cross-ledger trigger set while keeping every earlier
        # object intact so the upgrade must restore them rather than relying
        # on a fresh database's full schema creation.
        for trigger_name in v33_trigger_names:
            raw.execute(f'DROP TRIGGER "{trigger_name}"')
        raw.execute("DROP TABLE session_sidebar_v2_attempt_zero_resolutions")
        raw.execute("UPDATE schema_version SET version = 32")
        raw.commit()
    finally:
        raw.close()

    upgraded = hermes_state.SessionDB(db_path)
    try:
        conn = upgraded._conn
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 33
        assert tuple(
            conn.execute(
                "SELECT id, source, started_at FROM sessions WHERE id = ?",
                ("v32-preserved-session",),
            ).fetchone()
        ) == ("v32-preserved-session", "cli", 1000.0)
        assert tuple(
            conn.execute(
                "SELECT id, session_id, role, content FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        ) == (
            message_id,
            "v32-preserved-session",
            "user",
            "preserve through v33",
        )
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("session_sidebar_v2_attempt_zero_resolutions",),
        ).fetchone() is not None
        trigger_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        assert set(v33_trigger_names) <= trigger_names
        # The store's trust decision is strict about the exact complete trigger
        # sets, so an upgraded v32 database must be admitted as a sound ledger
        # authority rather than merely contain the new table.
        from session_bridge.store import SessionBridgeStore

        assert SessionBridgeStore._sidebar_terminal_resolution_ledger_is_valid(conn)
    finally:
        upgraded.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        assert [
            tuple(row)
            for row in reopened._conn.execute(
                "SELECT version FROM schema_version"
            ).fetchall()
        ] == [(33,)]
        assert reopened._conn.execute(
            "SELECT content FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()[0] == "preserve through v33"
        assert {
            row[0]
            for row in reopened._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        } >= set(v33_trigger_names)
    finally:
        reopened.close()


CLAUDE_CHARACTERIZATION_EVENT_COLUMNS = (
    "job_id",
    "event_kind",
    "operation_id",
    "source_session_id",
    "bridge_id",
    "idempotency_key",
    "reserved_claude_uuid",
    "evidence_digest",
    "created_at",
)

CLAUDE_CHARACTERIZATION_EVENT_UNIQUE_COLUMNS = {
    ("job_id", "event_kind"),
    ("operation_id", "event_kind"),
    ("source_session_id", "event_kind"),
    ("bridge_id", "event_kind"),
    ("idempotency_key", "event_kind"),
    ("reserved_claude_uuid", "event_kind"),
}

CLAUDE_CHARACTERIZATION_EVENT_TRIGGER_NAMES = {
    "trg_claude_characterization_event_identity",
    "trg_claude_characterization_cleanup_order",
    "trg_claude_characterization_abort_order",
    "trg_claude_characterization_event_no_update",
    "trg_claude_characterization_event_no_delete",
}


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.split())


def _characterization_trigger_sql(
    conn: sqlite3.Connection,
) -> dict[str, str]:
    return {
        row[0]: _normalized_sql(row[1])
        for row in conn.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE type = 'trigger'
                 AND tbl_name =
                     'session_claude_visibility_characterization_events'"""
        ).fetchall()
    }


def _characterization_event_rows(
    conn: sqlite3.Connection,
) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT "
            + ", ".join(CLAUDE_CHARACTERIZATION_EVENT_COLUMNS)
            + " FROM session_claude_visibility_characterization_events "
            "ORDER BY job_id, event_kind"
        ).fetchall()
    ]


def _characterization_unique_columns(
    conn: sqlite3.Connection,
) -> set[tuple[str, ...]]:
    column_sets = set()
    for index in conn.execute(
        "PRAGMA index_list('session_claude_visibility_characterization_events')"
    ).fetchall():
        if index[2] == 1:
            column_sets.add(
                tuple(
                    row[2]
                    for row in conn.execute(
                        f'PRAGMA index_info("{index[1]}")'
                    ).fetchall()
                )
            )
    return column_sets


def _characterization_foreign_keys(
    conn: sqlite3.Connection,
) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (row[3], row[4], row[2], row[6])
        for row in conn.execute(
            "PRAGMA foreign_key_list("
            "'session_claude_visibility_characterization_events')"
        ).fetchall()
    )


def _characterization_table_sql(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """SELECT sql FROM sqlite_master
           WHERE type = 'table'
             AND name =
                 'session_claude_visibility_characterization_events'"""
    ).fetchone()
    assert row is not None
    return row[0]


def _prepare_legacy_characterization_events_database(
    db_path: Path, *, schema_version: int
) -> tuple[list[tuple], dict[str, str], str]:
    current = hermes_state.SessionDB(db_path)
    try:
        current._conn.execute(
            """INSERT INTO session_claude_visibility_jobs (
                   id, source_session_id, bridge_id, idempotency_key,
                   reserved_claude_uuid, native_name, source_provider,
                   source_cwd, signed_marker, state, attempts,
                   next_attempt_at, eligible_at, created_at, updated_at
               ) VALUES (
                   'legacy-job', 'codex:legacy-operation', 'legacy-bridge',
                   'legacy-idempotency',
                   '11111111-1111-4111-8111-111111111111',
                   '[Codex] legacy characterization', 'codex', 'C:/legacy',
                   'legacy-signed-marker', 'claude_retry', 7,
                   100, 100, 100, 100
               )"""
        )
        current._conn.execute(
            """INSERT INTO session_claude_visibility_characterization_events (
                   job_id, event_kind, operation_id, source_session_id,
                   bridge_id, idempotency_key, reserved_claude_uuid,
                   evidence_digest, created_at
               ) VALUES (
                   'legacy-job', 'registered',
                   '22222222-2222-4222-8222-222222222222',
                   'codex:legacy-operation', 'legacy-bridge',
                   'legacy-idempotency',
                   '11111111-1111-4111-8111-111111111111', ?, 100.125
               )""",
            ("a" * 64,),
        )
        current._conn.commit()
        expected_rows = _characterization_event_rows(current._conn)
        expected_trigger_sql = _characterization_trigger_sql(current._conn)
    finally:
        current.close()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        for trigger_name in CLAUDE_CHARACTERIZATION_EVENT_TRIGGER_NAMES:
            conn.execute(f'DROP TRIGGER "{trigger_name}"')
        conn.executescript(
            """ALTER TABLE session_claude_visibility_characterization_events
                   RENAME TO
                       session_claude_visibility_characterization_events_new;
               CREATE TABLE session_claude_visibility_characterization_events (
                   job_id TEXT NOT NULL,
                   event_kind TEXT NOT NULL CHECK (
                       event_kind IN ('registered', 'cleanup_completed')
                   ),
                   operation_id TEXT NOT NULL,
                   source_session_id TEXT NOT NULL,
                   bridge_id TEXT NOT NULL,
                   idempotency_key TEXT NOT NULL,
                   reserved_claude_uuid TEXT NOT NULL,
                   evidence_digest TEXT NOT NULL CHECK (
                       length(evidence_digest) = 64
                       AND evidence_digest NOT GLOB '*[^0-9a-f]*'
                   ),
                   created_at REAL NOT NULL,
                   PRIMARY KEY (job_id, event_kind),
                   UNIQUE (operation_id, event_kind),
                   UNIQUE (source_session_id, event_kind),
                   UNIQUE (bridge_id, event_kind),
                   UNIQUE (idempotency_key, event_kind),
                   UNIQUE (reserved_claude_uuid, event_kind),
                   FOREIGN KEY (job_id, reserved_claude_uuid)
                       REFERENCES session_claude_visibility_jobs(
                           id, reserved_claude_uuid
                       ) ON DELETE RESTRICT
               );
               INSERT INTO session_claude_visibility_characterization_events
               SELECT *
               FROM session_claude_visibility_characterization_events_new;
               DROP TABLE
                   session_claude_visibility_characterization_events_new;
            """
        )
        for trigger_sql in expected_trigger_sql.values():
            conn.execute(trigger_sql)
        conn.execute(
            """DELETE FROM session_bridge_migrations
               WHERE migration_name = 'claude_characterization_events_v28'"""
        )
        if schema_version == 26:
            conn.execute(
                """DELETE FROM session_bridge_migrations
                   WHERE migration_name =
                       'claude_characterization_abort_max_attempts_v27'"""
            )
        conn.execute("UPDATE schema_version SET version = ?", (schema_version,))
        conn.commit()
        legacy_sql = conn.execute(
            """SELECT sql FROM sqlite_master
               WHERE type = 'table'
                 AND name =
                     'session_claude_visibility_characterization_events'"""
        ).fetchone()[0]
    finally:
        conn.close()

    return expected_rows, expected_trigger_sql, legacy_sql


@pytest.mark.parametrize("legacy_version", (26, 27))
def test_legacy_database_rebuilds_characterization_events_for_launch_abort(
    tmp_path, legacy_version
):
    db_path = tmp_path / f"v{legacy_version}-characterization-events.db"
    expected_rows, expected_trigger_sql, legacy_sql = (
        _prepare_legacy_characterization_events_database(
            db_path, schema_version=legacy_version
        )
    )
    assert "'launch_aborted'" not in _normalized_sql(legacy_sql)

    upgraded = hermes_state.SessionDB(db_path)
    try:
        table_sql = _characterization_table_sql(upgraded._conn)
        migrations = {
            row[0]
            for row in upgraded._conn.execute(
                """SELECT migration_name FROM session_bridge_migrations
                   WHERE migration_name LIKE 'claude_characterization_%'"""
            ).fetchall()
        }

        assert "'launch_aborted'" in _normalized_sql(table_sql)
        assert (
            upgraded._conn.execute("SELECT version FROM schema_version").fetchone()[0]
            == hermes_state.SCHEMA_VERSION
        )
        assert migrations == {
            "claude_characterization_abort_max_attempts_v27",
            "claude_characterization_events_v28",
            "claude_characterization_event_orphan_quarantine_v29",
        }
        assert _characterization_event_rows(upgraded._conn) == expected_rows
        assert _characterization_trigger_sql(upgraded._conn) == expected_trigger_sql
        assert set(expected_trigger_sql) == (
            CLAUDE_CHARACTERIZATION_EVENT_TRIGGER_NAMES
        )
        assert _characterization_unique_columns(upgraded._conn) == (
            CLAUDE_CHARACTERIZATION_EVENT_UNIQUE_COLUMNS
        )
        assert _characterization_foreign_keys(upgraded._conn) == (
            (
                "job_id",
                "id",
                "session_claude_visibility_jobs",
                "RESTRICT",
            ),
            (
                "reserved_claude_uuid",
                "reserved_claude_uuid",
                "session_claude_visibility_jobs",
                "RESTRICT",
            ),
        )
        assert (
            upgraded._conn.execute(
                """SELECT 1 FROM sqlite_master
               WHERE type = 'table'
                 AND name =
                     '_session_claude_visibility_characterization_events_v28'"""
            ).fetchone()
            is None
        )
        assert (
            upgraded._conn.execute(
                """PRAGMA foreign_key_check(
                   'session_claude_visibility_characterization_events'
               )"""
            ).fetchall()
            == []
        )

        with pytest.raises(sqlite3.IntegrityError, match="identity mismatch"):
            upgraded._conn.execute(
                """INSERT INTO
                       session_claude_visibility_characterization_events (
                           job_id, event_kind, operation_id, source_session_id,
                           bridge_id, idempotency_key, reserved_claude_uuid,
                           evidence_digest, created_at
                       ) VALUES (
                           'missing-job', 'registered', 'missing-operation',
                           'missing-session', 'missing-bridge',
                           'missing-idempotency',
                           '33333333-3333-4333-8333-333333333333', ?, 101
                       )""",
                ("b" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cleanup is not anchored"):
            upgraded._conn.execute(
                """INSERT INTO
                       session_claude_visibility_characterization_events (
                           job_id, event_kind, operation_id, source_session_id,
                           bridge_id, idempotency_key, reserved_claude_uuid,
                           evidence_digest, created_at
                       ) VALUES (
                           'legacy-job', 'cleanup_completed',
                           '33333333-3333-4333-8333-333333333333',
                           'codex:legacy-operation', 'legacy-bridge',
                           'legacy-idempotency',
                           '11111111-1111-4111-8111-111111111111', ?, 102
                       )""",
                ("c" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="abort is not anchored"):
            upgraded._conn.execute(
                """INSERT INTO
                       session_claude_visibility_characterization_events (
                           job_id, event_kind, operation_id, source_session_id,
                           bridge_id, idempotency_key, reserved_claude_uuid,
                           evidence_digest, created_at
                       ) VALUES (
                           'legacy-job', 'launch_aborted',
                           '44444444-4444-4444-8444-444444444444',
                           'codex:legacy-operation', 'legacy-bridge',
                           'legacy-idempotency',
                           '11111111-1111-4111-8111-111111111111', ?, 103
                       )""",
                ("d" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            upgraded._conn.execute(
                """UPDATE session_claude_visibility_characterization_events
                   SET created_at = 200 WHERE job_id = 'legacy-job'"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            upgraded._conn.execute(
                """DELETE FROM
                       session_claude_visibility_characterization_events
                   WHERE job_id = 'legacy-job'"""
            )
        upgraded._conn.rollback()
    finally:
        upgraded.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        assert _characterization_table_sql(reopened._conn) == table_sql
        assert _characterization_event_rows(reopened._conn) == expected_rows
        assert _characterization_trigger_sql(reopened._conn) == expected_trigger_sql
        assert (
            reopened._conn.execute(
                """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'claude_characterization_events_v28'"""
            ).fetchone()[0]
            == 1
        )
        assert (
            reopened._conn.execute(
                """SELECT 1 FROM sqlite_master
               WHERE type = 'table'
                 AND name =
                     '_session_claude_visibility_characterization_events_v28'"""
            ).fetchone()
            is None
        )
    finally:
        reopened.close()


def test_current_database_quarantines_orphan_characterization_events(tmp_path):
    """A v28 audit orphan must not prevent every bridge service restart."""

    db_path = tmp_path / "v28-characterization-event-orphan.db"
    current = hermes_state.SessionDB(db_path)
    try:
        current._conn.execute(
            "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
            ("preserve-source", "cli", 90.0),
        )
        current._conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) "
            "VALUES (?, ?, ?, ?)",
            ("preserve-source", "user", "preserve this session", 91.0),
        )
        current._conn.execute(
            """INSERT INTO session_claude_visibility_jobs (
                   id, source_session_id, bridge_id, idempotency_key,
                   reserved_claude_uuid, native_name, source_provider,
                   source_cwd, signed_marker, state, attempts,
                   next_attempt_at, eligible_at, created_at, updated_at
               ) VALUES (
                   'orphaned-characterization-job',
                   'codex:orphaned-characterization', 'orphaned-bridge',
                   'orphaned-idempotency',
                   '99999999-9999-4999-8999-999999999999',
                   '[Codex] orphaned characterization', 'codex', 'C:/orphaned',
                   'orphaned-signed-marker', 'claude_retry', 1,
                   100, 100, 100, 100
               )"""
        )
        current._conn.execute(
            """INSERT INTO session_claude_visibility_characterization_events (
                   job_id, event_kind, operation_id, source_session_id,
                   bridge_id, idempotency_key, reserved_claude_uuid,
                   evidence_digest, created_at
               ) VALUES (
                   'orphaned-characterization-job', 'registered',
                   '88888888-8888-4888-8888-888888888888',
                   'codex:orphaned-characterization', 'orphaned-bridge',
                   'orphaned-idempotency',
                   '99999999-9999-4999-8999-999999999999', ?, 100.125
               )""",
            ("e" * 64,),
        )
        current._conn.commit()
    finally:
        current.close()

    # Simulate the historical defect: an older writer deleted a parent with
    # foreign-key enforcement disabled, leaving only a non-operational audit row.
    damaged = sqlite3.connect(db_path)
    try:
        damaged.execute("PRAGMA foreign_keys=OFF")
        # That older writer predates trg_session_claude_visibility_jobs_no_delete,
        # which now refuses exactly this DELETE -- that guard is the point, so the
        # simulation has to step around it to reproduce the damage the quarantine
        # migration exists to clean up. Dropping it here is confined to this
        # throwaway fixture DB; the assertions below reopen through SessionDB,
        # whose _init_schema recreates the guard.
        damaged.execute(
            "DROP TRIGGER IF EXISTS trg_session_claude_visibility_jobs_no_delete"
        )
        damaged.execute(
            "DELETE FROM session_claude_visibility_jobs "
            "WHERE id = 'orphaned-characterization-job'"
        )
        damaged.execute(
            """DELETE FROM session_bridge_migrations
               WHERE migration_name =
                   'claude_characterization_event_orphan_quarantine_v29'"""
        )
        damaged.commit()
    finally:
        damaged.close()

    repaired = hermes_state.SessionDB(db_path)
    try:
        assert _characterization_event_rows(repaired._conn) == []
        assert [
            tuple(row)
            for row in repaired._conn.execute(
                """SELECT job_id, event_kind, operation_id, source_session_id,
                          bridge_id, idempotency_key, reserved_claude_uuid,
                          evidence_digest, created_at, reason
                     FROM session_claude_visibility_characterization_event_quarantine"""
            ).fetchall()
        ] == [
            (
                "orphaned-characterization-job",
                "registered",
                "88888888-8888-4888-8888-888888888888",
                "codex:orphaned-characterization",
                "orphaned-bridge",
                "orphaned-idempotency",
                "99999999-9999-4999-8999-999999999999",
                "e" * 64,
                100.125,
                "missing_parent_job",
            )
        ]
        assert [
            tuple(row)
            for row in repaired._conn.execute(
                "SELECT content FROM messages WHERE session_id = 'preserve-source'"
            ).fetchall()
        ] == [("preserve this session",)]
        assert repaired._conn.execute(
            """PRAGMA foreign_key_check(
                   'session_claude_visibility_characterization_events'
               )"""
        ).fetchall() == []
        assert repaired._conn.execute(
            """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'claude_characterization_event_orphan_quarantine_v29'"""
        ).fetchone()[0] == 1
    finally:
        repaired.close()


def test_current_database_quarantines_orphan_sidebar_resolution(tmp_path):
    """Legacy resolution evidence without a job must not block every delivery."""

    db_path = tmp_path / "sidebar-resolution-orphan.db"
    current = hermes_state.SessionDB(db_path)
    current.close()

    damaged = sqlite3.connect(db_path)
    try:
        damaged.execute("PRAGMA foreign_keys=OFF")
        damaged.execute(
            """INSERT INTO session_sidebar_terminal_resolutions (
                   job_id, idempotency_key, source_session_id, bridge_id,
                   codex_thread_id, failure_state, failure_code,
                   failure_attempts, failure_next_attempt_at,
                   failure_updated_at, resolution_code, evidence_kind,
                   evidence_version, evidence_digest, resolved_at
               ) VALUES (
                   'orphaned-sidebar-job', 'orphaned-sidebar-idempotency',
                   'claude:orphaned-sidebar-source', 'orphaned-sidebar-bridge',
                   'orphaned-codex-thread', 'sidebar_failed',
                   'native_create_ambiguous', 1, 100, 100,
                   'native_thread_unrecoverable',
                   'codex_app_server_read_not_loaded_resume_no_rollout',
                   1, ?, 100
               )""",
            ("f" * 64,),
        )
        damaged.execute(
            """DELETE FROM session_bridge_migrations
               WHERE migration_name =
                   'sidebar_resolution_orphan_quarantine_v30'"""
        )
        damaged.commit()
    finally:
        damaged.close()

    repaired = hermes_state.SessionDB(db_path)
    try:
        assert repaired._conn.execute(
            "SELECT COUNT(*) FROM session_sidebar_terminal_resolutions"
        ).fetchone()[0] == 0
        quarantined = repaired._conn.execute(
            """SELECT resolution_table, job_id, source_session_id, reason,
                      payload_json
                 FROM session_sidebar_orphan_resolution_quarantine"""
        ).fetchone()
        assert quarantined is not None
        assert tuple(quarantined[:4]) == (
            "session_sidebar_terminal_resolutions",
            "orphaned-sidebar-job",
            "claude:orphaned-sidebar-source",
            "missing_parent_job",
        )
        payload = json.loads(quarantined[4])
        assert payload["idempotency_key"] == "orphaned-sidebar-idempotency"
        assert payload["evidence_digest"] == "f" * 64
        for table_name in (
            "session_sidebar_terminal_resolutions",
            "session_sidebar_precreate_resolutions",
            "session_sidebar_unbound_resolutions",
        ):
            assert repaired._conn.execute(
                f"PRAGMA foreign_key_check({table_name})"
            ).fetchall() == []
        assert repaired._conn.execute(
            """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'sidebar_resolution_orphan_quarantine_v30'"""
        ).fetchone()[0] == 1
    finally:
        repaired.close()


def _insert_orphan_reconciliation_proof(db_path, digest):
    """Strand a proof exactly as the 2026-08-09 foreign_keys=OFF DELETE did."""

    damaged = sqlite3.connect(db_path)
    try:
        damaged.execute("PRAGMA foreign_keys=OFF")
        damaged.execute(
            """INSERT INTO session_sidebar_reconciliation_proofs (
                   proof_digest, job_id, source_session_id, bridge_id,
                   marker_digest, placement_generation, delivery_generation,
                   reconciliation_generation, completed_at, expires_at,
                   inventory_digest, state, match_count, recovered_thread_id,
                   fixed_reason, created_at
               ) VALUES (
                   ?, 'orphaned-proof-job', 'claude:orphaned-proof-source',
                   'sidebar:orphaned-proof-bridge', ?, 1, 1,
                   'codex:1785547162144598:generation', 100.5, 130.5,
                   ?, 'absence_proven', 0, NULL, NULL, 100.5
               )""",
            (digest, "a" * 64, "b" * 64),
        )
        damaged.execute(
            """DELETE FROM session_bridge_migrations
               WHERE migration_name =
                   'sidebar_reconciliation_proof_orphan_quarantine_v31'"""
        )
        damaged.commit()
    finally:
        damaged.close()


def test_current_database_quarantines_orphan_sidebar_reconciliation_proof(tmp_path):
    """A proof whose parent job vanished must move to quarantine intact."""

    db_path = tmp_path / "sidebar-reconciliation-proof-orphan.db"
    current = hermes_state.SessionDB(db_path)
    current.close()

    digest = "c" * 64
    _insert_orphan_reconciliation_proof(db_path, digest)

    repaired = hermes_state.SessionDB(db_path)
    try:
        assert repaired._conn.execute(
            "SELECT COUNT(*) FROM session_sidebar_reconciliation_proofs"
        ).fetchone()[0] == 0
        quarantined = repaired._conn.execute(
            """SELECT proof_digest, job_id, source_session_id, bridge_id,
                      marker_digest, placement_generation, delivery_generation,
                      reconciliation_generation, completed_at, expires_at,
                      inventory_digest, state, match_count, recovered_thread_id,
                      fixed_reason, created_at, reason
                 FROM session_sidebar_reconciliation_proof_quarantine"""
        ).fetchone()
        assert quarantined is not None
        # Every column is preserved verbatim -- this table is the only surviving
        # record of the reconciliation, so a lossy move would defeat the point.
        assert tuple(quarantined) == (
            digest,
            "orphaned-proof-job",
            "claude:orphaned-proof-source",
            "sidebar:orphaned-proof-bridge",
            "a" * 64,
            1,
            1,
            "codex:1785547162144598:generation",
            100.5,
            130.5,
            "b" * 64,
            "absence_proven",
            0,
            None,
            None,
            100.5,
            "missing_parent_job",
        )
        assert repaired._conn.execute(
            "PRAGMA foreign_key_check(session_sidebar_reconciliation_proofs)"
        ).fetchall() == []
        assert repaired._conn.execute(
            """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'sidebar_reconciliation_proof_orphan_quarantine_v31'"""
        ).fetchone()[0] == 1
    finally:
        repaired.close()


def test_reconciliation_proof_quarantine_restores_immutability_triggers(tmp_path):
    """The migration drops the guards to move rows; it must put them back."""

    db_path = tmp_path / "sidebar-reconciliation-proof-triggers.db"
    current = hermes_state.SessionDB(db_path)
    current.close()

    _insert_orphan_reconciliation_proof(db_path, "d" * 64)

    repaired = hermes_state.SessionDB(db_path)
    try:
        assert {
            row[0]
            for row in repaired._conn.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'trigger'
                     AND tbl_name = 'session_sidebar_reconciliation_proofs'"""
            ).fetchall()
        } == {
            "trg_sidebar_reconciliation_proofs_no_update",
            "trg_sidebar_reconciliation_proofs_no_delete",
        }

        # Arm the guards: a surviving proof must still be undeletable and
        # unupdatable. A migration that left the table unguarded would pass the
        # name check above only if it recreated them, but this proves they bite.
        repaired._conn.execute(
            """INSERT INTO sessions (id, source, started_at)
               VALUES ('guard-source', 'claude', 1)"""
        )
        repaired._conn.execute(
            """INSERT INTO session_sidebar_jobs (
                   id, idempotency_key, source_session_id, bridge_id, state,
                   attempts, next_attempt_at, eligible_at, created_at, updated_at
               ) VALUES (
                   'guard-job', 'guard-idempotency', 'guard-source',
                   'sidebar:guard-bridge', 'sidebar_pending', 0, 1, 1, 1, 1
               )"""
        )
        repaired._conn.execute(
            """INSERT INTO session_sidebar_reconciliation_proofs (
                   proof_digest, job_id, source_session_id, bridge_id,
                   marker_digest, placement_generation, delivery_generation,
                   reconciliation_generation, completed_at, expires_at,
                   inventory_digest, state, match_count, recovered_thread_id,
                   fixed_reason, created_at
               ) VALUES (
                   ?, 'guard-job', 'guard-source', 'sidebar:guard-bridge',
                   ?, 1, 1, 'codex:1:generation', 200.0, 230.0,
                   ?, 'absence_proven', 0, NULL, NULL, 200.0
               )""",
            ("e" * 64, "a" * 64, "b" * 64),
        )
        repaired._conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            repaired._conn.execute(
                "DELETE FROM session_sidebar_reconciliation_proofs "
                "WHERE proof_digest = ?",
                ("e" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            repaired._conn.execute(
                "UPDATE session_sidebar_reconciliation_proofs "
                "SET match_count = 1 WHERE proof_digest = ?",
                ("e" * 64,),
            )
        assert repaired._conn.execute(
            "SELECT COUNT(*) FROM session_sidebar_reconciliation_proofs"
        ).fetchone()[0] == 1
    finally:
        repaired.close()


def test_v28_characterization_event_rebuild_rolls_back_and_reopens(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "v27-characterization-events-rollback.db"
    expected_rows, expected_trigger_sql, legacy_sql = (
        _prepare_legacy_characterization_events_database(db_path, schema_version=27)
    )

    failing_db = object.__new__(hermes_state.SessionDB)
    failing_db._conn = sqlite3.connect(db_path)
    failing_db._conn.execute("PRAGMA foreign_keys=ON")

    def fail_after_table_rebuild(_cursor):
        raise RuntimeError("forced trigger rebuild failure")

    monkeypatch.setattr(
        failing_db,
        "_create_claude_characterization_event_triggers",
        fail_after_table_rebuild,
    )
    try:
        with pytest.raises(RuntimeError, match="forced trigger rebuild failure"):
            failing_db._apply_claude_characterization_events_v28_migration(
                failing_db._conn.cursor()
            )
        assert not failing_db._conn.in_transaction
    finally:
        failing_db._conn.close()

    conn = sqlite3.connect(db_path)
    try:
        assert _characterization_table_sql(conn) == legacy_sql
        assert _characterization_event_rows(conn) == expected_rows
        assert _characterization_trigger_sql(conn) == expected_trigger_sql
        assert _characterization_unique_columns(conn) == (
            CLAUDE_CHARACTERIZATION_EVENT_UNIQUE_COLUMNS
        )
        assert _characterization_foreign_keys(conn) == (
            (
                "job_id",
                "id",
                "session_claude_visibility_jobs",
                "RESTRICT",
            ),
            (
                "reserved_claude_uuid",
                "reserved_claude_uuid",
                "session_claude_visibility_jobs",
                "RESTRICT",
            ),
        )
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 27
        assert (
            conn.execute(
                """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'claude_characterization_events_v28'"""
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                """SELECT 1 FROM sqlite_master
               WHERE type = 'table'
                 AND name =
                     '_session_claude_visibility_characterization_events_v28'"""
            ).fetchone()
            is None
        )
    finally:
        conn.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        assert "'launch_aborted'" in _normalized_sql(
            _characterization_table_sql(reopened._conn)
        )
        assert _characterization_event_rows(reopened._conn) == expected_rows
        assert _characterization_trigger_sql(reopened._conn) == expected_trigger_sql
        assert (
            reopened._conn.execute(
                """SELECT COUNT(*) FROM session_bridge_migrations
               WHERE migration_name =
                   'claude_characterization_events_v28'"""
            ).fetchone()[0]
            == 1
        )
    finally:
        reopened.close()


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


def test_sidebar_placement_columns_are_additive_and_legacy_visibility_stays_unverified(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "legacy-sidebar-placement.db"
    db = hermes_state.SessionDB(db_path)
    try:
        _seed_sessions(db._conn, "source")
        _insert_sidebar_job(
            db._conn,
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
        conn.execute("ALTER TABLE session_sidebar_jobs DROP COLUMN placement_generation")
        conn.execute("ALTER TABLE session_sidebar_jobs DROP COLUMN placement_verified_at")
        conn.execute(
            "UPDATE schema_version SET version = ?",
            (hermes_state.SCHEMA_VERSION - 1,),
        )
        conn.commit()
    finally:
        conn.close()

    reopened = hermes_state.SessionDB(db_path)
    try:
        row = reopened._conn.execute(
            """SELECT placement_generation, placement_verified_at
               FROM session_sidebar_jobs WHERE id = 'sidebar-job-1'"""
        ).fetchone()
        assert tuple(row) == (None, None)
        assert reopened._conn.execute(
            "SELECT version FROM schema_version"
        ).fetchone()[0] == hermes_state.SCHEMA_VERSION
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
