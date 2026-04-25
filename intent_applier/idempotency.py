"""SQLite-backed idempotency tracker for the intent applier.

Stores idempotency keys of intents that have been applied so replays are no-ops.
Survives restarts via the on-disk DB; can be rehydrated from a `processed/`
directory of intent files if the DB is lost.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS applied_intents (
    idempotency_key TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class IdempotencyTracker:
    """Tracks which intent idempotency_keys have been successfully applied."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            check_same_thread=False,
        )
        self._lock = threading.Lock()
        self._conn.executescript(_SCHEMA)

    def is_applied(self, idempotency_key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM applied_intents WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            return cur.fetchone() is not None

    def mark_applied(self, idempotency_key: str, *, message_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO applied_intents (idempotency_key, message_id, applied_at) "
                "VALUES (?, ?, ?)",
                (idempotency_key, message_id, now),
            )

    def rehydrate_from_processed(self, processed_dir: Path) -> int:
        """Scan processed_dir for *.json files; mark each one's idempotency_key as applied.

        Returns the number of keys added (excluding duplicates already in DB).
        """
        count = 0
        if not processed_dir.exists():
            return 0
        for path in sorted(processed_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("idempotency rehydrate: skipping %s (%s)", path.name, exc)
                continue
            key = data.get("idempotency_key")
            mid = data.get("message_id") or "unknown"
            if not key:
                continue
            if not self.is_applied(key):
                self.mark_applied(key, message_id=mid)
                count += 1
        logger.info("idempotency rehydrate: added %d keys from %s", count, processed_dir)
        return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()
