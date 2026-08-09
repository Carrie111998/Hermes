"""SQLite WAL persistence limited to Phase 1 AgentOps control-plane state."""

from __future__ import annotations

import os
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from plugins.agentops.control.audit import audit_entry_hash, validate_audit_event
from plugins.agentops.control.events import canonical_json
from plugins.agentops.control.models import AppendResult, AuditEvent, EventEnvelope, StoreInspection


SCHEMA_VERSION = 1


class StoreMigrationError(RuntimeError):
    """A migration failure that must keep the daemon in safe observe-only mode."""


class StoreIntegrityError(RuntimeError):
    """Raised when the same event identity is reused for different content."""


class AgentOpsStore:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path = Path(path)
        self._connection = connection
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _transaction(self):
        return _Transaction(self._connection, self._lock)

    def journal_mode(self) -> str:
        with self._lock:
            return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def schema_version(self) -> int:
        with self._lock:
            row = self._connection.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
        if row is None:
            raise StoreMigrationError("schema version unavailable")
        return int(row[0])

    def append_event(self, event: EventEnvelope) -> AppendResult:
        event_json = canonical_json(event.to_dict())
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT OR IGNORE INTO events(event_id, event_hash, event_json, occurred_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.content_hash,
                    event_json,
                    event.occurred_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                existing = cursor.execute("SELECT event_hash FROM events WHERE event_id = ?", (event.event_id,)).fetchone()
                if existing is None or str(existing[0]) != event.content_hash:
                    raise StoreIntegrityError("event identity conflict")
            if inserted:
                self._append_audit_locked(
                    cursor,
                    AuditEvent.create(
                        actor_type="system",
                        actor_id="agentopsd",
                        action="event.append",
                        object_type="event",
                        object_id=event.event_id,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        metadata={"event_hash": event.content_hash},
                        after_hash=event.content_hash,
                    ),
                )
        return AppendResult(event_id=event.event_id, inserted=inserted, content_hash=event.content_hash)

    def _append_audit_locked(self, cursor: sqlite3.Cursor, event: AuditEvent) -> int:
        validate_audit_event(event)
        row = cursor.execute("SELECT sequence, entry_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = str(row[1]) if row else None
        next_sequence = int(row[0]) + 1 if row else 1
        payload = event.to_dict(previous_hash=previous_hash)
        entry_hash = audit_entry_hash(sequence=next_sequence, payload=payload)
        cursor.execute(
            "INSERT INTO audit_events(sequence, event_json, previous_hash, entry_hash) VALUES (?, ?, ?, ?)",
            (next_sequence, canonical_json(payload), previous_hash, entry_hash),
        )
        return next_sequence

    def append_audit(self, event: AuditEvent) -> int:
        with self._transaction() as cursor:
            return self._append_audit_locked(cursor, event)

    def verify_audit_chain(self) -> bool:
        try:
            with self._lock:
                rows = self._connection.execute(
                    "SELECT sequence, event_json, previous_hash, entry_hash FROM audit_events ORDER BY sequence"
                ).fetchall()
            previous_hash: str | None = None
            for sequence, event_json, recorded_previous, entry_hash in rows:
                if recorded_previous != previous_hash:
                    return False
                payload = json.loads(event_json)
                if audit_entry_hash(sequence=int(sequence), payload=payload) != entry_hash:
                    return False
                previous_hash = str(entry_hash)
            return True
        except (sqlite3.Error, ValueError, TypeError):
            return False

    def event_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def audit_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])

    def backup_to(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, sqlite3.connect(destination) as backup_connection:
            self._connection.backup(backup_connection)
        return destination

    def restore_from(self, source: Path) -> None:
        source = Path(source)
        if not source.is_file():
            raise StoreMigrationError("backup unavailable")
        temporary = self.path.with_suffix(self.path.suffix + ".restore")
        with sqlite3.connect(source) as source_connection, sqlite3.connect(temporary) as restore_connection:
            source_connection.backup(restore_connection)
        with self._lock:
            self._connection.close()
            for suffix in ("-wal", "-shm"):
                (Path(str(self.path) + suffix)).unlink(missing_ok=True)
            os.replace(temporary, self.path)
            self._connection = _connect(self.path)
            _validate_existing_schema(self._connection, self.path)


class _Transaction:
    def __init__(self, connection: sqlite3.Connection, lock: threading.RLock):
        self.connection = connection
        self.lock = lock
        self.cursor: sqlite3.Cursor | None = None

    def __enter__(self) -> sqlite3.Cursor:
        self.lock.acquire()
        self.cursor = self.connection.cursor()
        self.cursor.execute("BEGIN IMMEDIATE")
        return self.cursor

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            if self.cursor is not None:
                self.cursor.close()
            self.lock.release()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _create_schema_v1(connection: sqlite3.Connection) -> None:
    with connection:
        connection.execute("CREATE TABLE schema_migrations(version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
        connection.execute(
            "CREATE TABLE events(event_id TEXT PRIMARY KEY, event_hash TEXT NOT NULL, event_json TEXT NOT NULL, occurred_at TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE audit_events(sequence INTEGER PRIMARY KEY, event_json TEXT NOT NULL, previous_hash TEXT, entry_hash TEXT NOT NULL)"
        )
        connection.execute("CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def _validate_existing_schema(connection: sqlite3.Connection, path: Path) -> None:
    tables = _table_names(connection)
    if not tables:
        _create_schema_v1(connection)
        return
    if "schema_migrations" not in tables:
        raise StoreMigrationError("unrecognized existing database")
    row = connection.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
    if row is None or int(row[0]) != SCHEMA_VERSION:
        raise StoreMigrationError("unsupported schema version")
    required = {"events", "audit_events", "metadata"}
    if not required.issubset(tables):
        raise StoreMigrationError("incomplete schema")


def open_store(path: Path) -> AgentOpsStore:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path)
        _validate_existing_schema(connection, path)
    except StoreMigrationError:
        if connection is not None:
            connection.close()
        raise
    except (sqlite3.Error, OSError) as exc:
        if connection is not None:
            connection.close()
        raise StoreMigrationError("store migration failed") from exc
    assert connection is not None
    return AgentOpsStore(path, connection)


def inspect_store(path: Path) -> StoreInspection:
    """Inspect an existing Store through SQLite read-only mode without creating it."""
    path = Path(path)
    if not path.exists():
        return StoreInspection(exists=False, schema_version=None, audit_chain_valid=None, event_count=None)
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            version_row = connection.execute("SELECT version FROM schema_migrations LIMIT 1").fetchone()
            event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            chain_valid = _verify_chain_read_only(connection)
        finally:
            connection.close()
        return StoreInspection(
            exists=True,
            schema_version=int(version_row[0]) if version_row else None,
            audit_chain_valid=chain_valid,
            event_count=event_count,
        )
    except sqlite3.Error:
        return StoreInspection(exists=True, schema_version=None, audit_chain_valid=False, event_count=None)


def _verify_chain_read_only(connection: sqlite3.Connection) -> bool:
    try:
        previous_hash: str | None = None
        for sequence, event_json, recorded_previous, entry_hash in connection.execute(
            "SELECT sequence, event_json, previous_hash, entry_hash FROM audit_events ORDER BY sequence"
        ):
            if recorded_previous != previous_hash:
                return False
            payload = json.loads(event_json)
            if audit_entry_hash(sequence=int(sequence), payload=payload) != entry_hash:
                return False
            previous_hash = str(entry_hash)
        return True
    except (sqlite3.Error, ValueError, TypeError):
        return False
