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
from plugins.agentops.control.redaction import RedactionError, redact_signal, redact_value, verify_redacted_signal


OBSERVER_SCHEMA_VERSION = 2
OBSERVER_DATABASE_NAME = "observer.db"
_STORE_KIND = "agentops-observer-store"
_LEGACY_TABLES = frozenset({"schema_migrations", "target_snapshots", "signals", "collector_cursors"})
_CURRENT_TABLES = _LEGACY_TABLES | {
    "observer_metadata",
    "collection_runs",
    "signal_occurrences",
    "collection_run_signals",
    "source_cursors",
}


class ObserverStoreError(RuntimeError):
    """Raised before untrusted or non-AgentOps state can be changed."""


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


def _preflight_existing_database(path: Path) -> int:
    """Read schema/integrity before any writable SQLite connection or WAL change."""
    try:
        connection = _readonly_connection(path)
        try:
            integrity = connection.execute("PRAGMA integrity_check(1)").fetchone()
            if integrity is None or str(integrity[0]).lower() != "ok":
                raise ObserverStoreError("observer database integrity invalid")
            tables = _table_names(connection)
            if "schema_migrations" not in tables:
                raise ObserverStoreError("unmanaged observer database")
            row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            version = 0 if row is None or row[0] is None else int(row[0])
            if version == 1 and tables == _LEGACY_TABLES:
                return version
            if version != OBSERVER_SCHEMA_VERSION or tables != _CURRENT_TABLES:
                raise ObserverStoreError("unmanaged observer database")
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


class ObserverStore:
    """Transactional store for its fixed own database and redacted evidence."""

    def __init__(self, config: AgentOpsConfig) -> None:
        self.path = observer_database_path(config)
        self._lock = threading.RLock()
        existed = self.path.exists()
        prior_version = _preflight_existing_database(self.path) if existed else 0
        if not existed:
            _create_empty_database(self.path)
        try:
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._migrate(prior_version)
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
                (snapshot.target_id, snapshot.observed_at.isoformat(), payload, snapshot.collector_version),
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
                        batch.observation_id,
                        batch.target_id,
                        batch.collector,
                        batch.source_id,
                        batch.collected_at.isoformat(),
                        int(batch.health.healthy),
                        batch.health.reason,
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
                        """,
                        (
                            signal.signal_id,
                            signal.target_id,
                            signal.collector,
                            signal.signal_type,
                            signal.observed_at.isoformat(),
                            signal.severity,
                            signal.redaction_version,
                            payload,
                        ),
                    )
                    self._connection.execute(
                        """
                        INSERT INTO signal_occurrences(signal_id, first_seen, last_seen, occurrence_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(signal_id) DO UPDATE SET
                            last_seen=excluded.last_seen,
                            occurrence_count=signal_occurrences.occurrence_count + 1
                        """,
                        (signal.signal_id, signal.observed_at.isoformat(), signal.observed_at.isoformat()),
                    )
                    self._connection.execute(
                        "INSERT INTO collection_run_signals(observation_id, signal_id) VALUES (?, ?)",
                        (batch.observation_id, signal.signal_id),
                    )
                if batch.next_cursor is not None:
                    self._connection.execute(
                        """
                        INSERT INTO source_cursors(target_id, collector, source_id, inode, offset)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(target_id, collector, source_id) DO UPDATE SET
                            inode=excluded.inode,
                            offset=excluded.offset
                        """,
                        (
                            batch.target_id,
                            batch.collector,
                            batch.source_id,
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
