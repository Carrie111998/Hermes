"""Target SQLite evidence that never opens a target database handle."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    asset_source_id,
    target_allows_asset,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


def _regular_metadata(path: Path) -> dict[str, int] | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    return {"bytes": int(metadata.st_size), "inode": int(metadata.st_ino), "mtime_ns": int(metadata.st_mtime_ns)}


class SQLiteHealthCollector:
    """Observe target file metadata; integrity remains explicitly unknown."""

    name = "sqlite_health"

    def __init__(self, database_path: Path, *, min_interval_seconds: float = 0.0) -> None:
        if min_interval_seconds < 0:
            raise ValueError("invalid collector rate")
        self.database_path = Path(database_path)
        self.source_id = asset_source_id(self.database_path)
        self.min_interval_seconds = min_interval_seconds
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        if not target_allows_asset(target, self.database_path):
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
            self._last_collection = now
        database = _regular_metadata(self.database_path)
        if database is None:
            return failed_batch(target, self.name, "sqlite_path_rejected", source_id=self.source_id)
        observed_at = utc_now()
        signal = redact_signal(
            RawSignal(
                target_id=target.target_id,
                collector=self.name,
                signal_type="sqlite.metadata",
                observed_at=observed_at,
                payload={
                    "integrity": "unknown",
                    "database": database,
                    "wal": _regular_metadata(self.database_path.with_name(self.database_path.name + "-wal")),
                    "shm": _regular_metadata(self.database_path.with_name(self.database_path.name + "-shm")),
                },
            )
        )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=(signal,),
            health=CollectorHealth(healthy=False, reason="sqlite_integrity_unknown"),
            source_id=self.source_id,
        )
