"""Metadata-only journal, generation markers, and bounded write boundary."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Optional
from uuid import uuid4

from runtime_state.key_custody import AuthJsonKeyCustody, KeyUnavailable
from runtime_state.locking import LockTimeout, MaintenanceLock
from runtime_state.migrations import utc_timestamp
from runtime_state.retry_config import (
    DEFAULT_RETRY_CONFIG,
    RetryConfig,
    is_transient_sqlite_error,
)
from runtime_state.schema import (
    DIGEST_PARAMETER_ID,
    JOURNAL_DDL,
    JOURNAL_EVENT_VERSION,
    JOURNAL_MIGRATION_CHECKSUM,
    JOURNAL_SCHEMA_VERSION,
    ORIGIN_MARKERS,
    TABLE_BUSINESS_KEY,
)

LOCK_TIMEOUT = "LOCK_TIMEOUT"
WRITE_ABORT = "WRITE_ABORT"
KEY_UNAVAILABLE = "KEY_UNAVAILABLE"
GENERATION_MISMATCH = "GENERATION_MISMATCH"
WRITE_COUNTER_GAP = "WRITE_COUNTER_GAP"
RETRY_EXHAUSTED = "RETRY_EXHAUSTED"

_SAFE_ERROR_MESSAGES = {
    LOCK_TIMEOUT: "runtime-state maintenance lock timed out",
    RETRY_EXHAUSTED: "runtime-state write retry budget exhausted",
    WRITE_ABORT: "runtime-state transaction aborted",
    KEY_UNAVAILABLE: "runtime-state digest key is unavailable",
}

_OPERATION_CATEGORIES = frozenset({
    "create_session",
    "create_task",
    "create_approval",
    "create_compression",
    "claim_owner",
    "update_columns",
    "release_owner",
})


class JournalWriteError(RuntimeError):
    def __init__(self, code: str, message: str, *, metadata: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.metadata = dict(metadata or {})


@dataclass(frozen=True)
class JournalConnectionState:
    db_path: Path
    key_custody: AuthJsonKeyCustody
    writer_epoch: int
    retry_config: RetryConfig = DEFAULT_RETRY_CONFIG
    startup_locked: bool = False
    current_generation: int = 1


_CONNECTIONS: dict[int, JournalConnectionState] = {}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _replay_tuple(row: Optional[sqlite3.Row | tuple], columns: list[str]) -> Optional[tuple]:
    if row is None:
        return None
    return tuple(row[index] for index in range(len(columns)))


def _row(conn: sqlite3.Connection, table: str, profile: str, key: str) -> Optional[dict[str, Any]]:
    key_column = TABLE_BUSINESS_KEY[table]
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE profile_name = ? AND {key_column} = ?",
        (profile, key),
    )
    values = cursor.fetchone()
    if values is None:
        return None
    return dict(zip([item[0] for item in cursor.description], values))


def _replay_columns(table: str) -> list[str]:
    key = TABLE_BUSINESS_KEY[table]
    state = {
        "session_state": "status",
        "task_state": "status",
        "approval_state": "approval_status",
        "compression_state": "compression_status",
    }[table]
    columns = [key, "owner", "owner_version", "schema_version", state]
    if table == "task_state":
        columns.append("session_id")
    if table == "approval_state":
        columns.extend(["session_id", "task_id", "breaker_status"])
    if table == "compression_state":
        columns.append("task_id")
    return list(dict.fromkeys(columns))


def _same_replay(before: Optional[dict], after: Optional[dict], table: str) -> bool:
    if before is None or after is None:
        return before is after
    return all(before.get(column) == after.get(column) for column in _replay_columns(table))


def register_connection(
    conn: sqlite3.Connection,
    db_path: Path,
    *,
    writer_epoch: int = 0,
    key_custody: Optional[AuthJsonKeyCustody] = None,
    retry_config: RetryConfig = DEFAULT_RETRY_CONFIG,
) -> None:
    custody = key_custody or AuthJsonKeyCustody(db_path.with_name("auth.json"))
    _CONNECTIONS[id(conn)] = JournalConnectionState(
        db_path=db_path,
        key_custody=custody,
        writer_epoch=max(0, int(writer_epoch)),
        retry_config=retry_config,
    )


def unregister_connection(conn: sqlite3.Connection) -> None:
    _CONNECTIONS.pop(id(conn), None)


def connection_state(conn: sqlite3.Connection) -> JournalConnectionState:
    try:
        return _CONNECTIONS[id(conn)]
    except KeyError as exc:
        raise JournalWriteError(WRITE_ABORT, "unregistered runtime-state connection") from None


def journal_lock_path(conn: sqlite3.Connection) -> Path:
    return connection_state(conn).db_path.with_name(
        connection_state(conn).db_path.name + ".maintenance.lock"
    )


def _operation_category(operation: object) -> str:
    value = operation if isinstance(operation, str) else ""
    return value if value in _OPERATION_CATEGORIES else "unclassified"


def _failure_metadata(
    state: JournalConnectionState,
    operation: object,
    reason_code: str,
    *,
    attempt: int = 1,
    started: Optional[float] = None,
    digest: Optional[str] = None,
    before: Optional[dict[str, Any]] = None,
    after: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "operation_category": _operation_category(operation),
        "attempt": int(attempt),
        "max_attempts": state.retry_config.max_attempts,
        "elapsed_ms": max(0, int((time.monotonic() - started) * 1000)) if started else 0,
        "diagnostic_timestamp": utc_timestamp(),
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "writer_generation": state.current_generation,
        "writer_epoch": state.writer_epoch,
        "reason_code": reason_code,
    }
    if digest is not None:
        metadata["entity_digest"] = digest
    if before is not None:
        metadata["materialized_counter_before"] = int(before.get("materialized_write_counter", 0))
    if after is not None:
        metadata["materialized_counter_after"] = int(after.get("materialized_write_counter", 0))
    return metadata


def _safe_error_message(code: str) -> str:
    return _SAFE_ERROR_MESSAGES.get(code, "runtime-state transaction aborted")


def _rollback_safely(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


def ensure_journal_schema(conn: sqlite3.Connection) -> None:
    """Add ARCH-003 state markers without changing the ARCH-001 core version."""

    for table in TABLE_BUSINESS_KEY:
        columns = _table_columns(conn, table)
        for column, definition in (
            ("materialized_writer_generation", "INTEGER NOT NULL DEFAULT 0"),
            ("materialized_write_counter", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        tuple_columns = [
            column for column in _replay_columns(table)
            if column not in {TABLE_BUSINESS_KEY[table]}
        ]
        condition = " OR ".join(f"NEW.{column} IS NOT OLD.{column}" for column in tuple_columns)
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_materialized_insert_marker "
            f"AFTER INSERT ON {table} BEGIN UPDATE {table} SET "
            "materialized_writer_generation = COALESCE((SELECT current_generation "
            "FROM runtime_state_journal_meta WHERE id = 1), 1), "
            "materialized_write_counter = 1 WHERE rowid = NEW.rowid; END"
        )
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_materialized_update_marker "
            f"AFTER UPDATE ON {table} WHEN NEW.materialized_write_counter = "
            f"OLD.materialized_write_counter AND ({condition}) BEGIN UPDATE {table} SET "
            "materialized_writer_generation = COALESCE((SELECT current_generation "
            "FROM runtime_state_journal_meta WHERE id = 1), 1), "
            "materialized_write_counter = materialized_write_counter + 1 "
            "WHERE rowid = NEW.rowid; END"
        )

    conn.execute("CREATE TABLE IF NOT EXISTS runtime_state_journal_migrations ("
                 "version INTEGER PRIMARY KEY, description TEXT NOT NULL, "
                 "checksum_sha256 TEXT NOT NULL, applied_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS runtime_state_journal_meta ("
                 "id INTEGER PRIMARY KEY CHECK (id = 1), journal_schema_version INTEGER NOT NULL, "
                 "current_generation INTEGER NOT NULL, durable_writer_epoch INTEGER NOT NULL, "
                 "downgrade_unsafe INTEGER NOT NULL, transition_epoch INTEGER NOT NULL, "
                 "transition_at TEXT NOT NULL)")
    # The explicit statements below keep this routine compatible with DBs that
    # already contain the two metadata tables from a partial migration.
    conn.execute("CREATE TABLE IF NOT EXISTS runtime_state_journal ("
                 "event_id TEXT PRIMARY KEY, event_kind TEXT NOT NULL, profile_name TEXT NOT NULL, "
                 "entity_category TEXT NOT NULL, entity_digest TEXT NOT NULL, "
                 "digest_parameter_id TEXT NOT NULL, key_check TEXT NOT NULL, origin_marker TEXT NOT NULL, "
                 "origin_epoch INTEGER NOT NULL, origin_genesis_seq INTEGER NOT NULL, entity_seq INTEGER NOT NULL, "
                 "operation_category TEXT NOT NULL, lifecycle_state_before TEXT, lifecycle_state_after TEXT, "
                 "owner_version_before INTEGER, owner_version_after INTEGER, state_schema_version INTEGER NOT NULL, "
                 "journal_event_version INTEGER NOT NULL, writer_generation INTEGER NOT NULL, writer_epoch INTEGER NOT NULL, "
                 "materialized_write_counter_before INTEGER NOT NULL, materialized_write_counter_after INTEGER NOT NULL, "
                 "diagnostic_timestamp TEXT NOT NULL, baseline_sealed_lifecycle_state TEXT, "
                 "baseline_sealed_owner_version INTEGER, baseline_sealed_state_schema_version INTEGER, "
                 "sealed_through_seq INTEGER, sealed_materialized_write_counter INTEGER, "
                 "UNIQUE(profile_name, entity_category, entity_digest, entity_seq))")
    conn.execute("CREATE INDEX IF NOT EXISTS runtime_state_journal_entity_idx ON "
                 "runtime_state_journal(profile_name, entity_category, entity_digest, entity_seq)")
    now = utc_timestamp()
    conn.execute("INSERT OR IGNORE INTO runtime_state_journal_meta "
                 "(id, journal_schema_version, current_generation, durable_writer_epoch, "
                 "downgrade_unsafe, transition_epoch, transition_at) VALUES (1, ?, 1, 0, 0, 0, ?)",
                 (JOURNAL_SCHEMA_VERSION, now))
    conn.execute("INSERT OR IGNORE INTO runtime_state_journal_migrations "
                 "(version, description, checksum_sha256, applied_at) VALUES (1, ?, ?, ?)",
                 ("ARCH-003 metadata-only runtime journal", JOURNAL_MIGRATION_CHECKSUM, now))


def startup_transition(conn: sqlite3.Connection, *, writer_epoch: int = 0) -> JournalConnectionState:
    state = connection_state(conn)
    lock = MaintenanceLock(journal_lock_path(conn), exclusive=True)
    try:
        lock.acquire()
    except LockTimeout:
        updated = JournalConnectionState(
            state.db_path,
            state.key_custody,
            writer_epoch,
            state.retry_config,
            True,
            1,
        )
        _CONNECTIONS[id(conn)] = updated
        return updated
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT current_generation, durable_writer_epoch, downgrade_unsafe "
                           "FROM runtime_state_journal_meta WHERE id = 1").fetchone()
        if row is None:
            raise JournalWriteError(WRITE_ABORT, "journal metadata singleton is missing")
        generation, durable_epoch, unsafe = map(int, row)
        if writer_epoch > durable_epoch:
            generation += 1
            durable_epoch = writer_epoch
            unsafe = 0
        elif writer_epoch < durable_epoch:
            unsafe = 1
        else:
            unsafe = 0
        conn.execute("UPDATE runtime_state_journal_meta SET current_generation=?, durable_writer_epoch=?, "
                     "downgrade_unsafe=?, transition_epoch=?, transition_at=? WHERE id=1",
                     (generation, durable_epoch, unsafe, writer_epoch, utc_timestamp()))
        conn.commit()
        updated = JournalConnectionState(
            state.db_path,
            state.key_custody,
            writer_epoch,
            state.retry_config,
            False,
            generation,
        )
        _CONNECTIONS[id(conn)] = updated
        return updated
    except Exception:
        conn.rollback()
        raise
    finally:
        lock.release()


def _digest(key: bytes, *parts: object) -> str:
    payload = "\0".join(str(part) for part in parts).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]


def _append_event(conn: sqlite3.Connection, table: str, profile: str, business_key: str,
                  operation: str, before: Optional[dict], after: dict, key: bytes) -> None:
    state = connection_state(conn)
    digest = _digest(key, profile, table, business_key)
    key_check = _digest(key, "hermes-runtime-state-key-check-v1")
    latest = conn.execute("SELECT entity_seq, origin_marker, origin_epoch, origin_genesis_seq "
                          "FROM runtime_state_journal WHERE profile_name=? AND entity_category=? "
                          "AND entity_digest=? ORDER BY entity_seq DESC LIMIT 1",
                          (profile, table, digest)).fetchone()
    if latest is None:
        seq, marker, origin_epoch, origin_seq = 1, (
            "NEW_ENTITY_GENESIS" if before is None else "POST_MIGRATION_GENESIS"
        ), state.writer_epoch, 1
        if state.current_generation > 1:
            marker = "GENERATION_REORIGIN_GENESIS"
        kind = "genesis"
    else:
        seq = int(latest[0]) + 1
        marker, origin_epoch, origin_seq, kind = latest[1], int(latest[2]), int(latest[3]), "mutation"
    if marker not in ORIGIN_MARKERS:
        raise JournalWriteError(WRITE_ABORT, "journal origin marker is invalid")
    lifecycle = {
        "session_state": "status", "task_state": "status",
        "approval_state": "approval_status", "compression_state": "compression_status",
    }[table]
    conn.execute("INSERT INTO runtime_state_journal (event_id,event_kind,profile_name,entity_category,"
                 "entity_digest,digest_parameter_id,key_check,origin_marker,origin_epoch,origin_genesis_seq,"
                 "entity_seq,operation_category,lifecycle_state_before,lifecycle_state_after,owner_version_before,"
                 "owner_version_after,state_schema_version,journal_event_version,writer_generation,writer_epoch,"
                 "materialized_write_counter_before,materialized_write_counter_after,diagnostic_timestamp) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (uuid4().hex, kind, profile, table, digest, DIGEST_PARAMETER_ID, key_check, marker,
                  origin_epoch, origin_seq, seq, _operation_category(operation),
                  before.get(lifecycle) if before else None,
                  after.get(lifecycle), before.get("owner_version") if before else None,
                  after.get("owner_version"), after.get("schema_version"), JOURNAL_EVENT_VERSION,
                  after.get("materialized_writer_generation", state.current_generation), state.writer_epoch,
                  before.get("materialized_write_counter", 0) if before else 0,
                  after.get("materialized_write_counter", 0), utc_timestamp()))


def run_journaled_mutation(conn: sqlite3.Connection, table: str, profile: str,
                           business_key: str, operation: str,
                           callback: Callable[[], Any], *, key_required: bool = True) -> Any:
    state = connection_state(conn)
    operation_category = _operation_category(operation)
    if state.startup_locked:
        raise JournalWriteError(
            WRITE_ABORT,
            _safe_error_message(WRITE_ABORT),
            metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
        )
    try:
        with MaintenanceLock(journal_lock_path(conn), exclusive=False):
            # The named lock is intentionally acquired before the epoch check
            # and before BEGIN IMMEDIATE. An old writer therefore fails closed
            # without opening a SQLite write transaction.
            try:
                unsafe = conn.execute(
                    "SELECT downgrade_unsafe FROM runtime_state_journal_meta WHERE id=1"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise JournalWriteError(
                    WRITE_ABORT,
                    _safe_error_message(WRITE_ABORT),
                    metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
                ) from None
            if unsafe is None or int(unsafe[0]):
                raise JournalWriteError(
                    WRITE_ABORT,
                    _safe_error_message(WRITE_ABORT),
                    metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
                )

            if not state.key_custody.auth_path.exists():
                try:
                    history = conn.execute(
                        "SELECT 1 FROM runtime_state_journal LIMIT 1"
                    ).fetchone()
                except sqlite3.DatabaseError as exc:
                    raise JournalWriteError(
                        WRITE_ABORT,
                        _safe_error_message(WRITE_ABORT),
                        metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
                    ) from None
                if history is not None:
                    raise JournalWriteError(
                        KEY_UNAVAILABLE,
                        _safe_error_message(KEY_UNAVAILABLE),
                        metadata=_failure_metadata(state, operation_category, KEY_UNAVAILABLE),
                    )
            try:
                key = state.key_custody.ensure() if key_required else b""
            except KeyUnavailable as exc:
                raise JournalWriteError(
                    KEY_UNAVAILABLE,
                    _safe_error_message(KEY_UNAVAILABLE),
                    metadata=_failure_metadata(state, operation_category, KEY_UNAVAILABLE),
                ) from None

            digest = _digest(key, profile, table, business_key)
            for attempt in range(1, state.retry_config.max_attempts + 1):
                started = time.monotonic()
                before: Optional[dict[str, Any]] = None
                after: Optional[dict[str, Any]] = None
                commit_started = False
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    before = _row(conn, table, profile, business_key)
                    result = callback()
                    if not getattr(result, "success", False):
                        conn.rollback()
                        return result
                    after = _row(conn, table, profile, business_key)
                    if after is None:
                        raise JournalWriteError(
                            WRITE_ABORT,
                            _safe_error_message(WRITE_ABORT),
                        )
                    if not _same_replay(before, after, table):
                        _append_event(conn, table, profile, business_key, operation_category, before, after, key)
                    # A commit error is ambiguous: never replay the callback.
                    commit_started = True
                    conn.commit()
                    return result
                except JournalWriteError as exc:
                    _rollback_safely(conn)
                    code = exc.code if exc.code in _SAFE_ERROR_MESSAGES else WRITE_ABORT
                    raise JournalWriteError(
                        code,
                        _safe_error_message(code),
                        metadata=_failure_metadata(
                            state,
                            operation_category,
                            code,
                            attempt=attempt,
                            started=started,
                            digest=digest,
                            before=before,
                            after=after,
                        ),
                    ) from None
                except sqlite3.DatabaseError as exc:
                    _rollback_safely(conn)
                    transient = not commit_started and is_transient_sqlite_error(exc)
                    if transient and attempt < state.retry_config.max_attempts:
                        time.sleep(state.retry_config.delay_ms(attempt) / 1000.0)
                        continue
                    code = RETRY_EXHAUSTED if transient else WRITE_ABORT
                    raise JournalWriteError(
                        code,
                        _safe_error_message(code),
                        metadata=_failure_metadata(
                            state,
                            operation_category,
                            code,
                            attempt=attempt,
                            started=started,
                            digest=digest,
                            before=before,
                            after=after,
                        ),
                    ) from None
                except Exception as exc:
                    _rollback_safely(conn)
                    raise JournalWriteError(
                        WRITE_ABORT,
                        _safe_error_message(WRITE_ABORT),
                        metadata=_failure_metadata(
                            state,
                            operation_category,
                            WRITE_ABORT,
                            attempt=attempt,
                            started=started,
                            digest=digest,
                            before=before,
                            after=after,
                        ),
                    ) from None
    except LockTimeout as exc:
        _rollback_safely(conn)
        raise JournalWriteError(
            LOCK_TIMEOUT,
            _safe_error_message(LOCK_TIMEOUT),
            metadata=_failure_metadata(state, operation_category, LOCK_TIMEOUT),
        ) from None
    except JournalWriteError:
        raise
    except sqlite3.OperationalError as exc:
        _rollback_safely(conn)
        raise JournalWriteError(
            WRITE_ABORT,
            _safe_error_message(WRITE_ABORT),
            metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
        ) from None
    except Exception as exc:
        _rollback_safely(conn)
        raise JournalWriteError(
            WRITE_ABORT,
            _safe_error_message(WRITE_ABORT),
            metadata=_failure_metadata(state, operation_category, WRITE_ABORT),
        ) from None
