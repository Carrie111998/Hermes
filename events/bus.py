"""SQLite-backed Event Bus for the Hermes notification layer.

Provides emit/subscribe/ack/query operations with per-subscriber cursors
for independent fan-out consumption.  WAL mode enables concurrent reads
(subscribers) and writes (producers).
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    timestamp    TEXT NOT NULL,
    priority     TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    job_id       TEXT,
    tags         TEXT NOT NULL DEFAULT '[]',
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_type_status_ts
    ON events (event_type, status, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_source
    ON events (source, created_at);
CREATE INDEX IF NOT EXISTS idx_events_correlation
    ON events (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS subscriber_cursors (
    subscriber_id TEXT PRIMARY KEY,
    last_rowid    INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class EventBus:
    """SQLite-backed event bus with per-subscriber cursors.

    Thread-safe: uses a threading lock around all write operations
    and check_same_thread=False for cross-thread reads.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            from events.paths import events_db_path
            db_path = events_db_path()
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
                timeout=10,
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist, and migrate older schemas in-place."""
        conn = self._get_conn()

        # Check if events table exists with legacy schema (no status column).
        # If so, add status column BEFORE running the schema script so the new
        # (event_type, status, timestamp) index can be created successfully.
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
            if cols and "status" not in cols:
                logger.info("EventBus: migrating legacy schema (adding status column)")
                conn.execute("ALTER TABLE events ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
                conn.commit()
        except sqlite3.OperationalError:
            # Table doesn't exist yet — schema script will create it fresh
            pass

        conn.executescript(_SCHEMA_SQL)
        conn.commit()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write operation under the lock."""
        with self._lock:
            conn = self._get_conn()
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor

    def emit(
        self,
        event_type: EventType,
        source: str,
        payload: Dict[str, Any],
        priority: Optional[Priority] = None,
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Emit a new event into the bus.  Returns the event_id."""
        event = Event.create(
            event_type=event_type,
            source=source,
            payload=payload,
            priority=priority,
            correlation_id=correlation_id,
            job_id=job_id,
            tags=tags,
        )
        self._execute(
            """INSERT INTO events
               (event_id, event_type, source, timestamp, priority,
                payload, correlation_id, job_id, tags)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.event_type.type_string,
                event.source,
                event.timestamp,
                event.priority.label,
                json.dumps(event.payload),
                event.correlation_id,
                event.job_id,
                json.dumps(event.tags),
            ),
        )
        logger.debug("Event emitted: %s from %s [%s]",
                      event.event_type.type_string, source, event.priority.label)
        return event.event_id

    def subscribe(
        self,
        subscriber_id: str,
        event_types: Optional[List[EventType]] = None,
        min_priority: Optional[Priority] = None,
    ) -> List[Event]:
        """Fetch events since this subscriber's last cursor position.

        Does NOT advance the cursor -- call ack() after processing.
        """
        conn = self._get_conn()

        # Get subscriber's cursor (last processed rowid)
        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
        last_rowid = row["last_rowid"] if row else 0

        # Build query with optional filters
        conditions = ["rowid > ?"]
        params: list = [last_rowid]

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(et.type_string for et in event_types)

        if min_priority:
            # Map priority labels to those at or above the threshold
            valid = [p.label for p in Priority if p.level >= min_priority.level]
            placeholders = ",".join("?" for _ in valid)
            conditions.append(f"priority IN ({placeholders})")
            params.extend(valid)

        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY rowid ASC",
            params,
        ).fetchall()

        return [self._row_to_event(r) for r in rows]

    def ack(self, subscriber_id: str, event_ids: List[str]) -> None:
        """Advance subscriber cursor past the given events.

        The cursor is set to the max rowid among the acked events.
        """
        if not event_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in event_ids)
        row = conn.execute(
            f"SELECT MAX(rowid) as max_rowid FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        ).fetchone()
        if row and row["max_rowid"] is not None:
            self._execute(
                """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(subscriber_id)
                   DO UPDATE SET last_rowid = excluded.last_rowid,
                                updated_at = excluded.updated_at""",
                (subscriber_id, row["max_rowid"]),
            )

    def query(
        self,
        event_type: Optional[EventType] = None,
        source: Optional[str] = None,
        since: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> List[Event]:
        """Ad-hoc query for events (no cursor tracking)."""
        conn = self._get_conn()
        conditions = []
        params: list = []

        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type.type_string)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if since:
            conditions.append("timestamp >= ?")
            params.append(since)
        if correlation_id:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY rowid ASC",
            params,
        ).fetchall()

        return [self._row_to_event(r) for r in rows]

    def checkpoint(self) -> None:
        """Run a passive WAL checkpoint so external readers see recent data."""
        with self._lock:
            try:
                self._get_conn().execute("PRAGMA wal_checkpoint(PASSIVE)")
            except sqlite3.Error as e:
                logger.warning("WAL checkpoint failed: %s", e)

    def close(self) -> None:
        """Close the thread-local SQLite connection."""
        conn = getattr(self._local, "conn", None)
        if conn:
            conn.close()
            self._local.conn = None

    def subscriber_lag(self, subscriber_id: str) -> int:
        """Return the count of events the subscriber hasn't processed yet.

        Lag = (total events emitted) - (cursor position).  A subscriber
        that has never polled returns the full event count.  Useful for
        monitoring: a growing lag indicates a subscriber is falling
        behind or has crashed.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
        last_rowid = row["last_rowid"] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) as n FROM events WHERE rowid > ?",
            (last_rowid,),
        ).fetchone()
        return int(row["n"]) if row else 0

    def cleanup(self, retention_days: int = 30) -> int:
        """Remove events older than retention_days.  Returns count removed."""
        cursor = self._execute(
            "DELETE FROM events WHERE created_at < datetime('now', ? || ' days')",
            (f"-{retention_days}",),
        )
        removed = cursor.rowcount
        if removed:
            logger.info("EventBus cleanup: removed %d events older than %d days",
                        removed, retention_days)
        return removed

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        """Convert a SQLite Row to an Event instance."""
        event_type = EventType.from_string(row["event_type"])
        if event_type is None:
            raise ValueError(f"Unknown event type in DB: {row['event_type']}")
        return Event(
            event_id=row["event_id"],
            event_type=event_type,
            source=row["source"],
            timestamp=row["timestamp"],
            priority=Priority.from_string(row["priority"]),
            payload=json.loads(row["payload"]),
            correlation_id=row["correlation_id"],
            job_id=row["job_id"],
            tags=json.loads(row["tags"]),
        )
