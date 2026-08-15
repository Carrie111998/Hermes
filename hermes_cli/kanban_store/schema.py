"""Versioned Kanban security schema and in-place migration.

The migration extends the existing board database.  It does not create a
parallel event source or a second task/run authority.  Existing ``task_events``
rows are copied in their committed order into the canonical ordered table.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterable

from .database import write_txn

SCHEMA_VERSION = 1

_BASE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS kanban_security_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finalizations (
    finalization_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    finalization_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    artifact_set_sha256 TEXT NOT NULL,
    intent_set_sha256 TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(task_id, run_id, claim_generation)
);

CREATE TABLE IF NOT EXISTS run_artifact_declarations (
    declaration_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(task_id, run_id, claim_generation, relative_path)
);

CREATE TABLE IF NOT EXISTS run_artifacts (
    artifact_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    display_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    blob_path TEXT NOT NULL,
    frozen_at INTEGER NOT NULL,
    UNIQUE(task_id, run_id, claim_generation, relative_path),
    UNIQUE(task_id, run_id, claim_generation, sha256, display_name)
);

CREATE TABLE IF NOT EXISTS publication_intents (
    intent_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    required INTEGER NOT NULL CHECK(required IN (0, 1)),
    state TEXT NOT NULL,
    publisher_principal TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    target_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    headers_json TEXT NOT NULL,
    marker TEXT NOT NULL,
    prepared_bytes BLOB NOT NULL,
    request_body_bytes BLOB NOT NULL,
    request_body_sha256 TEXT NOT NULL,
    wire_sha256 TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(task_id, run_id, claim_generation, intent_id),
    UNIQUE(wire_sha256, publisher_principal)
);

CREATE TABLE IF NOT EXISTS publication_intent_ledger (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_uuid TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_approvals (
    approval_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    wire_sha256 TEXT NOT NULL,
    target_sha256 TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    publisher_principal TEXT NOT NULL,
    actor TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE(intent_id, wire_sha256, actor)
);

CREATE TABLE IF NOT EXISTS publication_approval_ledger (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_uuid TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_dispatches (
    dispatch_id TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    state TEXT NOT NULL,
    claimed_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS publication_dispatch_ledger (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_uuid TEXT NOT NULL UNIQUE,
    dispatch_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_receipts (
    receipt_id TEXT PRIMARY KEY,
    dispatch_id TEXT NOT NULL UNIQUE,
    intent_id TEXT NOT NULL,
    disposition TEXT NOT NULL,
    remote_identity TEXT,
    status_code INTEGER,
    detail_code TEXT,
    response_digest TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_receipt_ledger (
    ledger_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_uuid TEXT NOT NULL UNIQUE,
    receipt_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS publication_reconciliations (
    reconciliation_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    dispatch_id TEXT NOT NULL,
    state TEXT NOT NULL,
    outcome TEXT,
    match_count INTEGER,
    detail_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS run_operations (
    operation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    closed_at INTEGER,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_scopes (
    scope_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    coverage TEXT NOT NULL,
    identity_json TEXT NOT NULL,
    freeze_supported INTEGER NOT NULL CHECK(freeze_supported IN (0, 1)),
    registered_at INTEGER NOT NULL,
    closed_at INTEGER
);

CREATE TABLE IF NOT EXISTS run_baselines (
    baseline_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    filesystem_sha256 TEXT NOT NULL,
    vcs_sha256 TEXT NOT NULL,
    attachment_sha256 TEXT NOT NULL,
    exclusions_sha256 TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(task_id, run_id, claim_generation)
);

CREATE TABLE IF NOT EXISTS run_observations (
    observation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    source TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    fresh_until INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reclaim_probes (
    probe_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    state TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER,
    UNIQUE(task_id, run_id, claim_generation, probe_id)
);

CREATE TABLE IF NOT EXISTS event_spool_imports (
    event_uuid TEXT PRIMARY KEY,
    spool_path_digest TEXT NOT NULL,
    imported_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_finalizations_run
    ON finalizations(task_id, run_id, claim_generation);
CREATE INDEX IF NOT EXISTS idx_artifacts_run
    ON run_artifacts(task_id, run_id, claim_generation);
CREATE INDEX IF NOT EXISTS idx_intents_run
    ON publication_intents(task_id, run_id, claim_generation, required, state);
CREATE INDEX IF NOT EXISTS idx_intents_state
    ON publication_intents(state, created_at);
CREATE INDEX IF NOT EXISTS idx_dispatches_state
    ON publication_dispatches(state, claimed_at);
CREATE INDEX IF NOT EXISTS idx_observations_run
    ON run_observations(task_id, run_id, claim_generation, observed_at);
"""

_EVENT_TABLE_SQL = """
CREATE TABLE {name} (
    event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uuid TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    claim_generation INTEGER,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    severity TEXT NOT NULL,
    retention_class TEXT NOT NULL,
    correlation_id TEXT,
    operation_id TEXT,
    stream TEXT,
    stream_seq INTEGER,
    host_committed_at INTEGER NOT NULL,
    producer_time INTEGER,
    payload_json TEXT NOT NULL
)
"""

_APPEND_ONLY_TABLES = (
    "finalizations",
    "publication_intent_ledger",
    "publication_approval_ledger",
    "publication_dispatch_ledger",
    "publication_receipt_ledger",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _add_column(conn: sqlite3.Connection, table: str, declaration: str) -> None:
    column = declaration.split()[0]
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {declaration}")


def _meta(conn: sqlite3.Connection, key: str, default_factory) -> str:
    row = conn.execute(
        "SELECT value FROM kanban_security_meta WHERE key=?", (key,)
    ).fetchone()
    if row:
        return str(row[0])
    value = str(default_factory())
    conn.execute(
        "INSERT INTO kanban_security_meta(key, value) VALUES (?, ?)", (key, value)
    )
    return value


def _rebuild_events(conn: sqlite3.Connection, board_id: str) -> None:
    if not _table_exists(conn, "task_events"):
        conn.execute(_EVENT_TABLE_SQL.format(name="task_events"))
        return
    cols = _columns(conn, "task_events")
    if "event_seq" in cols and "event_uuid" in cols and "event_type" in cols:
        return

    conn.execute("DROP TABLE IF EXISTS task_events_v1_ordered")
    conn.execute(_EVENT_TABLE_SQL.format(name="task_events_v1_ordered"))
    legacy_rows = conn.execute("SELECT * FROM task_events ORDER BY id").fetchall()
    for row in legacy_rows:
        keys = set(row.keys()) if hasattr(row, "keys") else cols
        legacy_id = int(row["id"])
        payload = row["payload"] if "payload" in keys else None
        if not payload:
            payload = "{}"
        else:
            try:
                json.loads(payload)
            except Exception:
                payload = json.dumps({"legacy_payload": str(payload)}, separators=(",", ":"))
        event_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"hermes-kanban:{board_id}:legacy-event:{legacy_id}")
        )
        conn.execute(
            """
            INSERT INTO task_events_v1_ordered(
                event_seq, event_uuid, task_id, run_id, claim_generation,
                schema_version, event_type, source, severity, retention_class,
                correlation_id, operation_id, stream, stream_seq,
                host_committed_at, producer_time, payload_json
            ) VALUES (?, ?, ?, ?, NULL, 1, ?, 'legacy', 'info', 'audit',
                      NULL, NULL, NULL, NULL, ?, NULL, ?)
            """,
            (
                legacy_id,
                event_uuid,
                row["task_id"],
                row["run_id"] if "run_id" in keys else None,
                row["kind"] if "kind" in keys else "legacy",
                int(row["created_at"]),
                payload,
            ),
        )
    conn.execute("DROP TABLE task_events")
    conn.execute("ALTER TABLE task_events_v1_ordered RENAME TO task_events")


def _migrate_publication_intents(conn: sqlite3.Connection) -> None:
    """Upgrade packet-previews that predate stored application bytes."""

    if not _table_exists(conn, "publication_intents"):
        return
    columns = _columns(conn, "publication_intents")
    if "headers_json" not in columns:
        conn.execute("ALTER TABLE publication_intents ADD COLUMN headers_json TEXT")
    if "request_body_bytes" not in columns:
        conn.execute("ALTER TABLE publication_intents ADD COLUMN request_body_bytes BLOB")
    if "request_body_sha256" not in columns:
        conn.execute("ALTER TABLE publication_intents ADD COLUMN request_body_sha256 TEXT")


def _install_append_only_triggers(conn: sqlite3.Connection) -> None:
    for table in _APPEND_ONLY_TABLES:
        for operation in ("UPDATE", "DELETE"):
            trigger = f"deny_{operation.lower()}_{table}"
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )


def migrate(conn: sqlite3.Connection) -> dict[str, str | int]:
    """Migrate one board database to the zero-authority V1 contract."""

    with write_txn(conn):
        conn.executescript(_BASE_TABLES_SQL)
        board_id = _meta(conn, "board_id", uuid.uuid4)
        database_id = _meta(conn, "database_id", uuid.uuid4)
        _meta(conn, "claim_hash_salt", lambda: secrets.token_hex(32))
        _meta(conn, "cursor_hmac_key", lambda: secrets.token_hex(32))
        _meta(conn, "schema_version", lambda: SCHEMA_VERSION)

        if _table_exists(conn, "tasks"):
            _add_column(conn, "tasks", "claim_generation INTEGER NOT NULL DEFAULT 0")
            _add_column(conn, "tasks", "claim_token_hash TEXT")
            _add_column(conn, "tasks", "publication_state TEXT")
        if _table_exists(conn, "task_runs"):
            _add_column(conn, "task_runs", "claim_generation INTEGER NOT NULL DEFAULT 0")
            _add_column(conn, "task_runs", "claim_token_hash TEXT")
            _add_column(conn, "task_runs", "worker_context_digest TEXT")
            _add_column(conn, "task_runs", "runtime_provider TEXT")
            _add_column(conn, "task_runs", "runtime_model TEXT")
            _add_column(conn, "task_runs", "runtime_api_mode TEXT")
            _add_column(conn, "task_runs", "runtime_session_id TEXT")
            _add_column(conn, "task_runs", "runtime_identity_source TEXT")
            _add_column(conn, "task_runs", "finalized_at INTEGER")

        _migrate_publication_intents(conn)
        _rebuild_events(conn, board_id)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_task_seq "
            "ON task_events(task_id, event_seq)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_events_run_seq "
            "ON task_events(run_id, event_seq)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_task_events_stream_seq "
            "ON task_events(stream, stream_seq) "
            "WHERE stream IS NOT NULL AND stream_seq IS NOT NULL"
        )
        _install_append_only_triggers(conn)
        now = int(time.time())
        conn.execute(
            "INSERT OR REPLACE INTO kanban_security_meta(key, value) VALUES ('migrated_at', ?)",
            (str(now),),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "board_id": board_id,
        "database_id": database_id,
    }


def meta_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute(
        "SELECT value FROM kanban_security_meta WHERE key=?", (key,)
    ).fetchone()
    if not row:
        raise KeyError(key)
    return str(row[0])


def schema_digest(conn: sqlite3.Connection) -> str:
    rows: Iterable[sqlite3.Row] = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type, name"
    )
    encoded = json.dumps(
        [(row[0], row[1], row[2]) for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
