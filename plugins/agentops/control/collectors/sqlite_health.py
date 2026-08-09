"""Read-only SQLite health facts, including adjacent WAL/SHM file metadata."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from urllib.parse import quote

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


def _regular_size(path: Path) -> int | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    return int(metadata.st_size)


class SQLiteHealthCollector:
    name = "sqlite_health"

    def __init__(self, database_path: Path) -> None:
        self.database_path = Path(database_path)

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        database_size = _regular_size(self.database_path)
        if database_size is None:
            return failed_batch(target, self.name, "sqlite_path_rejected")
        uri = "file:" + quote(os.fspath(self.database_path.absolute())) + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                integrity_row = connection.execute("PRAGMA integrity_check(1)").fetchone()
                page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
                page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            finally:
                connection.close()
        except sqlite3.Error:
            return failed_batch(target, self.name, "sqlite_read_failed")
        integrity = "" if integrity_row is None else str(integrity_row[0])
        observed_at = utc_now()
        signal = redact_signal(
            RawSignal(
                target_id=target.target_id,
                collector=self.name,
                signal_type="sqlite.health",
                observed_at=observed_at,
                severity="info" if integrity == "ok" else "warning",
                payload={
                    "integrity": integrity,
                    "page_count": page_count,
                    "page_size": page_size,
                    "database_bytes": database_size,
                    "wal_bytes": _regular_size(self.database_path.with_name(self.database_path.name + "-wal")),
                    "shm_bytes": _regular_size(self.database_path.with_name(self.database_path.name + "-shm")),
                },
            )
        )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=(signal,),
            health=CollectorHealth(healthy=integrity == "ok", reason=None if integrity == "ok" else "sqlite_integrity_failed"),
        )
