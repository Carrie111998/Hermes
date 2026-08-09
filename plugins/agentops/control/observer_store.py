"""Preflighted SQLite WAL persistence for redacted Phase 2 evidence only."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path
from urllib.parse import quote

from plugins.agentops.control.config import AgentOpsConfig, STATE_MARKER, _marker_is_valid
from plugins.agentops.control.observer_models import CollectionBatch, LogCursor, RawSignal, Signal, TargetSnapshot, thaw_value
from plugins.agentops.control.redaction import RedactionError, contains_secret, redact_signal, redact_text, redact_value, verify_redacted_signal


OBSERVER_SCHEMA_VERSION = 2
OBSERVER_DATABASE_NAME = "observer.db"
_STORE_KIND = "agentops-observer-store"
_STORE_PATH_LOCK = threading.RLock()
_LEGACY_TABLES = frozenset({"schema_migrations", "target_snapshots", "signals", "collector_cursors"})
_CURRENT_TABLES = _LEGACY_TABLES | {
    "observer_metadata",
    "collection_runs",
    "signal_occurrences",
    "collection_run_signals",
    "source_cursors",
}

_SCHEMA_COLUMNS = {
    "schema_migrations": (("version", "INTEGER", 0, 1),),
    "target_snapshots": (("target_id", "TEXT", 0, 1), ("observed_at", "TEXT", 1, 0), ("facts_json", "TEXT", 1, 0), ("collector_version", "TEXT", 1, 0)),
    "signals": (("signal_id", "TEXT", 0, 1), ("target_id", "TEXT", 1, 0), ("collector", "TEXT", 1, 0), ("signal_type", "TEXT", 1, 0), ("observed_at", "TEXT", 1, 0), ("severity", "TEXT", 1, 0), ("redaction_version", "INTEGER", 1, 0), ("payload_json", "TEXT", 1, 0)),
    "collector_cursors": (("target_id", "TEXT", 1, 1), ("collector", "TEXT", 1, 2), ("inode", "INTEGER", 1, 0), ("offset", "INTEGER", 1, 0)),
    "observer_metadata": (("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)),
    "collection_runs": (("observation_id", "TEXT", 0, 1), ("target_id", "TEXT", 1, 0), ("collector", "TEXT", 1, 0), ("source_id", "TEXT", 1, 0), ("collected_at", "TEXT", 1, 0), ("healthy", "INTEGER", 1, 0), ("reason", "TEXT", 0, 0), ("signal_count", "INTEGER", 1, 0)),
    "signal_occurrences": (("signal_id", "TEXT", 0, 1), ("first_seen", "TEXT", 1, 0), ("last_seen", "TEXT", 1, 0), ("occurrence_count", "INTEGER", 1, 0)),
    "collection_run_signals": (("observation_id", "TEXT", 1, 1), ("signal_id", "TEXT", 1, 2)),
    "source_cursors": (("target_id", "TEXT", 1, 1), ("collector", "TEXT", 1, 2), ("source_id", "TEXT", 1, 3), ("inode", "INTEGER", 1, 0), ("offset", "INTEGER", 1, 0)),
}


class ObserverStoreError(RuntimeError):
    """Raised before untrusted or non-AgentOps state can be changed."""


def _safe_text(value: object, field: str) -> str:
    text = redact_text(str(value))
    if contains_secret(text):
        raise ObserverStoreError(f"unredacted persisted field: {field}")
    return text


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _validate_state_root(config: AgentOpsConfig) -> Path:
    if not isinstance(config, AgentOpsConfig) or not config.state_dir_safe:
        raise ObserverStoreError("unsafe observer state directory")
    root = config.state_dir
    marker = root / STATE_MARKER
    try:
        root_meta = root.lstat()
        marker_meta = marker.lstat()
    except OSError as exc:
        raise ObserverStoreError("observer state directory unavailable") from exc
    if (
        root.is_symlink()
        or marker.is_symlink()
        or not stat.S_ISDIR(root_meta.st_mode)
        or not stat.S_ISREG(marker_meta.st_mode)
        or root_meta.st_uid != os.getuid()
        or marker_meta.st_uid != os.getuid()
        or _mode(root) != 0o700
        or _mode(marker) != 0o600
        or not _marker_is_valid(root)
    ):
        raise ObserverStoreError("unsafe observer state directory")
    return root


def observer_database_path(config: AgentOpsConfig) -> Path:
    """Return the only permitted local path after state-root preflight."""
    root = _validate_state_root(config)
    candidate = root / OBSERVER_DATABASE_NAME
    if candidate.parent != root or candidate.name != OBSERVER_DATABASE_NAME:
        raise ObserverStoreError("unsafe observer database path")
    if candidate.exists() or candidate.is_symlink():
        try:
            metadata = candidate.lstat()
        except OSError as exc:
            raise ObserverStoreError("unsafe observer database path") from exc
        if (
            candidate.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ObserverStoreError("unsafe observer database path")
    else:
        for sidecar in (candidate.with_name(candidate.name + "-wal"), candidate.with_name(candidate.name + "-shm")):
            if sidecar.exists() or sidecar.is_symlink():
                raise ObserverStoreError("unexpected observer database sidecar")
    return candidate


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(os.fspath(path.absolute())) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> frozenset[str]:
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return frozenset(str(row[0]) for row in rows)


def _validate_schema_shape(connection: sqlite3.Connection, version: int) -> None:
    tables = _LEGACY_TABLES if version == 1 else _CURRENT_TABLES
    for table in tables:
        rows = tuple((str(r[1]), str(r[2]).upper(), int(r[3]), int(r[5])) for r in connection.execute(f"PRAGMA table_info({table})"))
        if rows != _SCHEMA_COLUMNS[table]:
            raise ObserverStoreError("observer database schema incompatible")
    expected_fk = {
        ("signal_occurrences", "signals", "signal_id", "signal_id"),
        ("collection_run_signals", "collection_runs", "observation_id", "observation_id"),
        ("collection_run_signals", "signals", "signal_id", "signal_id"),
    }
    actual_fk = set()
    for table in tables:
        for row in connection.execute(f"PRAGMA foreign_key_list({table})"):
            actual_fk.add((table, str(row[2]), str(row[3]), str(row[4])))
    if actual_fk != (expected_fk if version == 2 else set()):
        raise ObserverStoreError("observer database constraints incompatible")
    for table in tables:
        for row in connection.execute(f"PRAGMA index_list({table})"):
            if int(row[2]) and str(row[3]) != "pk":
                raise ObserverStoreError("observer database indexes incompatible")
    objects = [(str(row[0]), str(row[1])) for row in connection.execute(
        "SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    )]
    expected_objects = [("table", table) for table in sorted(tables)]
    if objects != expected_objects:
        raise ObserverStoreError("observer database sqlite_master objects incompatible")
    all_objects = connection.execute("SELECT type,name FROM sqlite_master").fetchall()
    allowed_auto = {f"sqlite_autoindex_{table}_" for table in tables}
    for kind, name in all_objects:
        if str(kind) == "index" and str(name).startswith("sqlite_autoindex_"):
            if not any(str(name).startswith(prefix) for prefix in allowed_auto):
                raise ObserverStoreError("observer database unknown index")
        elif str(kind) not in {"table", "index"} or str(name).startswith("sqlite_"):
            if str(name) not in {table for _, table in expected_objects}:
                raise ObserverStoreError("observer database unknown object")


def _scan_database_secrets(connection: sqlite3.Connection) -> None:
    for table in _table_names(connection):
        columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
        for row in connection.execute(f"SELECT {','.join(columns)} FROM {table}"):
            for value in row:
                if not isinstance(value, str):
                    continue
                candidate = value
                try:
                    candidate = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
                if contains_secret(candidate):
                    raise ObserverStoreError("legacy database contains secret material")


def _preflight_existing_database(path: Path, identity: tuple[tuple[int, int], ...] | None = None) -> int:
    """Read schema/integrity before any writable SQLite connection or WAL change."""
    try:
        _verify_database_path(path, identity[0] if identity else None)
        connection = _readonly_connection(path)
        try:
            _verify_database_path(path, identity[0] if identity else None)
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise ObserverStoreError("observer database integrity invalid")
            tables = _table_names(connection)
            if "schema_migrations" not in tables:
                raise ObserverStoreError("unmanaged observer database")
            versions = [int(row[0]) for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            version = versions[-1] if versions else 0
            if identity and _database_identity(path) != identity:
                raise ObserverStoreError("observer database identity changed")
            if version == 1 and versions == [1] and tables == _LEGACY_TABLES:
                _validate_schema_shape(connection, version)
                _scan_database_secrets(connection)
                return version
            if version != OBSERVER_SCHEMA_VERSION or versions != [1, 2] or tables != _CURRENT_TABLES:
                raise ObserverStoreError("unmanaged observer database")
            _validate_schema_shape(connection, version)
            kind = connection.execute(
                "SELECT value FROM observer_metadata WHERE key='store_kind'"
            ).fetchone()
            if kind is None or kind[0] != _STORE_KIND:
                raise ObserverStoreError("unmanaged observer database")
            return version
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ObserverStoreError("observer database preflight failed") from exc


def _create_empty_database(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        raise ObserverStoreError("observer database creation failed") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def _database_identity(path: Path) -> tuple[tuple[int, int], ...]:
    identities = []
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if candidate.exists() or candidate.is_symlink():
            meta = candidate.lstat()
            if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_uid != os.getuid() or _mode(candidate) != 0o600:
                raise ObserverStoreError("observer database identity unsafe")
            identities.append((meta.st_dev, meta.st_ino))
        else:
            identities.append((-1, -1))
    return tuple(identities)


def _verify_database_path(path: Path, identity: tuple[int, int] | None = None) -> None:
    meta = path.lstat()
    if stat.S_ISLNK(meta.st_mode) or not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or _mode(path) != 0o600 or meta.st_nlink != 1:
        raise ObserverStoreError("observer database identity changed")
    if identity is not None and (meta.st_dev, meta.st_ino) != identity:
        raise ObserverStoreError("observer database identity changed")
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            sm = sidecar.lstat()
            if stat.S_ISLNK(sm.st_mode) or not stat.S_ISREG(sm.st_mode) or sm.st_uid != os.getuid() or sm.st_nlink != 1:
                raise ObserverStoreError("observer database sidecar unsafe")


class ObserverStore:
    """Transactional store for its fixed own database and redacted evidence."""

    def __init__(self, config: AgentOpsConfig) -> None:
        self.path = observer_database_path(config)
        self._lock = threading.RLock()
        existed = self.path.exists()
        identity = _database_identity(self.path) if existed else None
        prior_version = _preflight_existing_database(self.path, identity) if existed else 0
        if prior_version == 1:
            raise ObserverStoreError("legacy observer migration disabled without fd-bound handle")
        if not existed:
            _create_empty_database(self.path)
            identity = _database_identity(self.path)
        try:
          with _STORE_PATH_LOCK:
            if _database_identity(self.path) != identity:
                raise ObserverStoreError("observer database identity changed")
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            # No PRAGMA, migration, WAL switch, or write is allowed until the
            # path is revalidated after connect. If an attacker swapped the
            # pathname during connect, close immediately and fail closed.
            if _database_identity(self.path) != identity:
                self._connection.close()
                raise ObserverStoreError("observer database identity changed after connect")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._migrate(prior_version)
            if existed and _database_identity(self.path) != identity:
                raise ObserverStoreError("observer database identity changed")
            self._connection.execute("PRAGMA journal_mode=WAL")
            os.chmod(self.path, 0o600)
            if _mode(self.path) != 0o600:
                raise ObserverStoreError("observer database permissions invalid")
        except Exception:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise

    def _migrate(self, prior_version: int) -> None:
        with self._connection:
            if prior_version == 0:
                self._connection.executescript(
                    """
                    CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
                    CREATE TABLE target_snapshots (
                        target_id TEXT PRIMARY KEY,
                        observed_at TEXT NOT NULL,
                        facts_json TEXT NOT NULL,
                        collector_version TEXT NOT NULL
                    );
                    CREATE TABLE signals (
                        signal_id TEXT PRIMARY KEY,
                        target_id TEXT NOT NULL,
                        collector TEXT NOT NULL,
                        signal_type TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        redaction_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE collector_cursors (
                        target_id TEXT NOT NULL,
                        collector TEXT NOT NULL,
                        inode INTEGER NOT NULL,
                        offset INTEGER NOT NULL,
                        PRIMARY KEY (target_id, collector)
                    );
                    """
                )
                prior_version = 1
                self._connection.execute("INSERT INTO schema_migrations(version) VALUES (1)")
            if prior_version == 1:
                self._connection.executescript(
                    """
                    CREATE TABLE observer_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE collection_runs (
                        observation_id TEXT PRIMARY KEY,
                        target_id TEXT NOT NULL,
                        collector TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        collected_at TEXT NOT NULL,
                        healthy INTEGER NOT NULL,
                        reason TEXT,
                        signal_count INTEGER NOT NULL
                    );
                    CREATE TABLE signal_occurrences (
                        signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
                        first_seen TEXT NOT NULL,
                        last_seen TEXT NOT NULL,
                        occurrence_count INTEGER NOT NULL
                    );
                    CREATE TABLE collection_run_signals (
                        observation_id TEXT NOT NULL REFERENCES collection_runs(observation_id),
                        signal_id TEXT NOT NULL REFERENCES signals(signal_id),
                        PRIMARY KEY (observation_id, signal_id)
                    );
                    CREATE TABLE source_cursors (
                        target_id TEXT NOT NULL,
                        collector TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        inode INTEGER NOT NULL,
                        offset INTEGER NOT NULL,
                        PRIMARY KEY (target_id, collector, source_id)
                    );
                    """
                )
                self._connection.execute(
                    "INSERT INTO observer_metadata(key, value) VALUES ('store_kind', ?)", (_STORE_KIND,)
                )
                self._connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
            current = self._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if current != OBSERVER_SCHEMA_VERSION:
                raise ObserverStoreError("observer database version invalid")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def journal_mode(self) -> str:
        with self._lock:
            row = self._connection.execute("PRAGMA journal_mode").fetchone()
            return "" if row is None else str(row[0]).lower()

    def record_target_snapshot(self, snapshot: TargetSnapshot) -> None:
        if not isinstance(snapshot, TargetSnapshot):
            raise ObserverStoreError("invalid target snapshot")
        safe_facts = redact_value(thaw_value(snapshot.facts))
        payload = json.dumps(safe_facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO target_snapshots(target_id, observed_at, facts_json, collector_version)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    observed_at=excluded.observed_at,
                    facts_json=excluded.facts_json,
                    collector_version=excluded.collector_version
                WHERE excluded.observed_at >= target_snapshots.observed_at
                """,
                (_safe_text(snapshot.target_id, "target_id"), _safe_text(snapshot.observed_at.isoformat(), "observed_at"), payload, _safe_text(snapshot.collector_version, "collector_version")),
            )

    @staticmethod
    def _safe_signals(batch: CollectionBatch) -> tuple[Signal, ...]:
        safe_signals = tuple(
            redact_signal(
                RawSignal(
                    target_id=signal.target_id,
                    collector=signal.collector,
                    signal_type=signal.signal_type,
                    observed_at=signal.observed_at,
                    payload=signal.payload,
                    severity=signal.severity,
                )
            )
            for signal in batch.signals
        )
        for signal in safe_signals:
            verify_redacted_signal(signal)
        return tuple({signal.signal_id: signal for signal in safe_signals}.values())

    def commit_collection(self, batch: CollectionBatch) -> None:
        """Persist a collection run, recurring signals and source cursor atomically."""
        if not isinstance(batch, CollectionBatch):
            raise ObserverStoreError("invalid collection batch")
        if not batch.source_id:
            raise ObserverStoreError("collection source is required")
        try:
            safe_signals = self._safe_signals(batch)
        except RedactionError as exc:
            raise ObserverStoreError("unredacted collection rejected") from exc
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT INTO collection_runs(
                        observation_id, target_id, collector, source_id, collected_at, healthy, reason, signal_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _safe_text(batch.observation_id, "observation_id"),
                        _safe_text(batch.target_id, "target_id"),
                        _safe_text(batch.collector, "collector"),
                        _safe_text(batch.source_id, "source_id"),
                        _safe_text(batch.collected_at.isoformat(), "collected_at"),
                        int(batch.health.healthy),
                        None if batch.health.reason is None else _safe_text(batch.health.reason, "reason"),
                        len(safe_signals),
                    ),
                )
                for signal in safe_signals:
                    payload = json.dumps(thaw_value(signal.payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    self._connection.execute(
                        """
                        INSERT INTO signals(
                            signal_id, target_id, collector, signal_type, observed_at,
                            severity, redaction_version, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(signal_id) DO UPDATE SET
                            observed_at=excluded.observed_at,
                            severity=excluded.severity,
                            redaction_version=excluded.redaction_version,
                            payload_json=excluded.payload_json
                        WHERE excluded.observed_at >= signals.observed_at
                        """,
                        (
                            _safe_text(signal.signal_id, "signal_id"),
                            _safe_text(signal.target_id, "target_id"),
                            _safe_text(signal.collector, "collector"),
                            _safe_text(signal.signal_type, "signal_type"),
                            _safe_text(signal.observed_at.isoformat(), "observed_at"),
                            _safe_text(signal.severity, "severity"),
                            signal.redaction_version,
                            payload,
                        ),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO signal_occurrences(signal_id, first_seen, last_seen, occurrence_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(signal_id) DO UPDATE SET
                            first_seen=MIN(signal_occurrences.first_seen, excluded.first_seen),
                            last_seen=MAX(signal_occurrences.last_seen, excluded.last_seen),
                            occurrence_count=signal_occurrences.occurrence_count + 1
                        """,
                        (_safe_text(signal.signal_id, "signal_id"), _safe_text(signal.observed_at.isoformat(), "first_seen"), _safe_text(signal.observed_at.isoformat(), "last_seen")),
                    )
                    self._connection.execute(
                        "INSERT INTO collection_run_signals(observation_id, signal_id) VALUES (?, ?)",
                        (batch.observation_id, signal.signal_id),
                    )
                if batch.next_cursor is not None:
                    latest = self._connection.execute(
                        "SELECT MAX(collected_at) FROM collection_runs WHERE target_id=? AND collector=? AND source_id=? AND observation_id<>?",
                        (batch.target_id, batch.collector, batch.source_id, batch.observation_id),
                    ).fetchone()[0]
                    cursor_is_newer = latest is None or batch.collected_at.isoformat() >= str(latest)
                    existing_cursor = self._connection.execute(
                        "SELECT inode, offset FROM source_cursors WHERE target_id=? AND collector=? AND source_id=?",
                        (batch.target_id, batch.collector, batch.source_id),
                    ).fetchone()
                    if not cursor_is_newer:
                        self._connection.commit()
                        return
                    if existing_cursor is not None and int(existing_cursor[0]) == batch.next_cursor.inode and batch.next_cursor.offset < int(existing_cursor[1]):
                        self._connection.execute(
                            "UPDATE source_cursors SET offset=? WHERE target_id=? AND collector=? AND source_id=?",
                            (batch.next_cursor.offset, batch.target_id, batch.collector, batch.source_id),
                        )
                        self._connection.commit()
                        return
                    self._connection.execute(
                        """
                        INSERT INTO source_cursors(target_id, collector, source_id, inode, offset)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(target_id, collector, source_id) DO UPDATE SET
                            inode=excluded.inode,
                            offset=excluded.offset
                        WHERE excluded.inode != source_cursors.inode OR excluded.offset >= source_cursors.offset
                        """,
                        (
                            _safe_text(batch.target_id, "target_id"),
                            _safe_text(batch.collector, "collector"),
                            _safe_text(batch.source_id, "source_id"),
                            batch.next_cursor.inode,
                            batch.next_cursor.offset,
                        ),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_cursor(self, target_id: str, collector: str, source_id: str = "") -> LogCursor | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT inode, offset, source_id FROM source_cursors WHERE target_id=? AND collector=? AND source_id=?",
                (target_id, collector, source_id),
            ).fetchone()
            if row is None and not source_id:
                row = self._connection.execute(
                    "SELECT inode, offset, '' FROM collector_cursors WHERE target_id=? AND collector=?",
                    (target_id, collector),
                ).fetchone()
        return None if row is None else LogCursor(inode=int(row[0]), offset=int(row[1]), source_id=str(row[2]))

    def signal_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0])

    def collection_run_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0])

    def occurrence_count(self, signal_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT occurrence_count FROM signal_occurrences WHERE signal_id=?", (signal_id,)
            ).fetchone()
        return 0 if row is None else int(row[0])

    def snapshot_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM target_snapshots").fetchone()[0])


def open_observer_store(config: AgentOpsConfig) -> ObserverStore:
    return ObserverStore(config)
