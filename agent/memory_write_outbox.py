"""Profile-scoped durable outbox for external memory-provider mirrors."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict


class MemoryWriteOutbox:
    """Persist failed provider writes and replay them in FIFO order."""

    def __init__(self, hermes_home: str | Path, *, max_entries_per_provider: int = 1000) -> None:
        self._path = Path(hermes_home) / "memories" / "provider_write_outbox.sqlite3"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_entries = max(1, int(max_entries_per_provider))
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(provider, fingerprint)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_alerts (
                    provider TEXT PRIMARY KEY,
                    last_alert_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_provider_fifo "
                "ON pending_writes(provider, id)"
            )

    @staticmethod
    def _fingerprint(
        action: str,
        target: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> str:
        # Provenance (session/tool-call ids) changes when the agent retries the
        # same intent. Only old_text affects replace/remove write semantics.
        payload = json.dumps(
            [action, target, content, metadata.get("old_text", "")],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def enqueue(
        self,
        provider: str,
        action: str,
        target: str,
        content: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Insert one write intent, deduplicate it, and enforce the bound."""
        metadata = dict(metadata or {})
        metadata_json = json.dumps(metadata, sort_keys=True, ensure_ascii=False, default=str)
        fingerprint = self._fingerprint(action, target, content, metadata)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO pending_writes
                    (provider, fingerprint, action, target, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (provider, fingerprint, action, target, content, metadata_json, time.time()),
            )
            inserted = cursor.rowcount == 1
            before = conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE provider = ?", (provider,)
            ).fetchone()[0]
            conn.execute(
                """
                DELETE FROM pending_writes
                WHERE provider = ? AND id NOT IN (
                    SELECT id FROM pending_writes
                    WHERE provider = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (provider, provider, self._max_entries),
            )
            after = conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE provider = ?", (provider,)
            ).fetchone()[0]
        return {
            "queued": True,
            "deduplicated": not inserted,
            "dropped": max(0, before - after),
        }

    def pending_count(self, provider: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE provider = ?", (provider,)
            ).fetchone()
        return int(row[0])

    def replay(
        self,
        provider: str,
        deliver: Callable[[str, str, str, Dict[str, Any]], None],
    ) -> Dict[str, Any]:
        """Replay queued writes until the provider fails, preserving FIFO order."""
        replayed = 0
        with self._lock:
            while True:
                with self._connect() as conn:
                    row = conn.execute(
                        """
                        SELECT id, action, target, content, metadata_json
                        FROM pending_writes WHERE provider = ? ORDER BY id LIMIT 1
                        """,
                        (provider,),
                    ).fetchone()
                if row is None:
                    self.clear_alert(provider)
                    return {"replayed": replayed, "remaining": 0, "error": ""}
                try:
                    metadata = json.loads(row["metadata_json"])
                    deliver(row["action"], row["target"], row["content"], metadata)
                except Exception as exc:
                    error = str(exc)
                    with self._connect() as conn:
                        conn.execute(
                            """
                            UPDATE pending_writes
                            SET attempts = attempts + 1, last_error = ? WHERE id = ?
                            """,
                            (error, row["id"]),
                        )
                    return {
                        "replayed": replayed,
                        "remaining": self.pending_count(provider),
                        "error": error,
                    }
                with self._connect() as conn:
                    conn.execute("DELETE FROM pending_writes WHERE id = ?", (row["id"],))
                replayed += 1

    def should_alert(self, provider: str, cooldown_seconds: float) -> bool:
        """Persistently rate-limit alerts across short-lived agent instances."""
        now = time.time()
        cooldown = max(0.0, float(cooldown_seconds))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_alert_at FROM provider_alerts WHERE provider = ?", (provider,)
            ).fetchone()
            if row is not None and now - float(row[0]) < cooldown:
                return False
            conn.execute(
                """
                INSERT INTO provider_alerts(provider, last_alert_at) VALUES (?, ?)
                ON CONFLICT(provider) DO UPDATE SET last_alert_at = excluded.last_alert_at
                """,
                (provider, now),
            )
        return True

    def clear_alert(self, provider: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM provider_alerts WHERE provider = ?", (provider,))
