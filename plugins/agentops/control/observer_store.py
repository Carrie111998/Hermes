"""SQLite WAL persistence for redacted Phase 2 evidence only."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from pathlib import Path

from plugins.agentops.control.config import AgentOpsConfig
from plugins.agentops.control.observer_models import CollectionBatch, LogCursor, RawSignal, Signal, TargetSnapshot
from plugins.agentops.control.redaction import RedactionError, redact_signal, redact_value, verify_redacted_signal


OBSERVER_SCHEMA_VERSION = 1
OBSERVER_DATABASE_NAME = "observer.db"


class ObserverStoreError(RuntimeError):
    """Raised when observer evidence cannot remain AgentOps-owned and local."""


def observer_database_path(config: AgentOpsConfig) -> Path:
    """Return the sole permitted observer database path for a safe state root."""
    if not isinstance(config, AgentOpsConfig) or not config.state_dir_safe:
        raise ObserverStoreError("unsafe observer state directory")
    root = config.state_dir
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ObserverStoreError("observer state directory unavailable") from exc
    if root.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise ObserverStoreError("unsafe observer state directory")
    candidate = root / OBSERVER_DATABASE_NAME
    if candidate.parent != root or candidate.name != OBSERVER_DATABASE_NAME or candidate.is_symlink():
        raise ObserverStoreError("unsafe observer database path")
    return candidate


class ObserverStore:
    """Transactional store that never accepts target database paths or raw data."""

    def __init__(self, config: AgentOpsConfig) -> None:
        self.path = observer_database_path(config)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._migrate()
        except Exception:
            self._connection.close()
            raise

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
            )
            row = self._connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            current = 0 if row is None or row[0] is None else int(row[0])
            if current > OBSERVER_SCHEMA_VERSION:
                raise ObserverStoreError("observer database version is newer than supported")
            if current == 0:
                self._connection.executescript(
                    """
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
                self._connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)", (OBSERVER_SCHEMA_VERSION,)
                )

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
        safe_facts = redact_value(snapshot.facts)
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

    def commit_collection(self, batch: CollectionBatch) -> None:
        """Commit a whole redacted batch and its next cursor together."""
        if not isinstance(batch, CollectionBatch):
            raise ObserverStoreError("invalid collection batch")
        try:
            safe_signals: tuple[Signal, ...] = tuple(
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
        except RedactionError as exc:
            raise ObserverStoreError("unredacted collection rejected") from exc
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                for signal in safe_signals:
                    self._connection.execute(
                        """
                        INSERT OR IGNORE INTO signals(
                            signal_id, target_id, collector, signal_type, observed_at,
                            severity, redaction_version, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            signal.signal_id,
                            signal.target_id,
                            signal.collector,
                            signal.signal_type,
                            signal.observed_at.isoformat(),
                            signal.severity,
                            signal.redaction_version,
                            json.dumps(dict(signal.payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                        ),
                    )
                if batch.next_cursor is not None:
                    self._connection.execute(
                        """
                        INSERT INTO collector_cursors(target_id, collector, inode, offset)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(target_id, collector) DO UPDATE SET
                            inode=excluded.inode,
                            offset=excluded.offset
                        """,
                        (batch.target_id, batch.collector, batch.next_cursor.inode, batch.next_cursor.offset),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get_cursor(self, target_id: str, collector: str) -> LogCursor | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT inode, offset FROM collector_cursors WHERE target_id=? AND collector=?",
                (target_id, collector),
            ).fetchone()
        return None if row is None else LogCursor(inode=int(row[0]), offset=int(row[1]))

    def signal_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM signals").fetchone()[0])

    def snapshot_count(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COUNT(*) FROM target_snapshots").fetchone()[0])


def open_observer_store(config: AgentOpsConfig) -> ObserverStore:
    return ObserverStore(config)
