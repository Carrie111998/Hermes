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

CREATE TABLE IF NOT EXISTS dead_letters (
    event_id       TEXT NOT NULL,
    subscriber_id  TEXT NOT NULL,
    error          TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 1,
    first_failed_at TEXT NOT NULL DEFAULT (datetime('now')),
    failed_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, subscriber_id)
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_failed_at
    ON dead_letters (failed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dead_letters_subscriber
    ON dead_letters (subscriber_id, failed_at DESC);

CREATE TABLE IF NOT EXISTS handled_events (
    subscriber_id TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    handled_at    TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (subscriber_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_handled_events_event_id
    ON handled_events (event_id);
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
            # SR-446 / ADR-0018 — explicit WAL-tuning PRAGMAs. Rationale:
            # SR-409 flood on 2026-04-19 left event_bus.db-wal at 69 MB.
            # Root cause: PASSIVE checkpoints silently skip under reader
            # contention × 8 subscribers polling × stdlib sqlite3 implicit
            # transactions. Fix is three explicit PRAGMAs per connection:
            #   synchronous=NORMAL         — FULL is overkill with WAL
            #   journal_size_limit=32MiB   — was unbounded (-1)
            #   wal_autocheckpoint=1000    — was implicit default, now explicit
            # Do NOT lower wal_autocheckpoint below 1000 — it increases
            # skipped checkpoints under reader contention (anti-pattern per
            # research 12 / ADR-0018). Pinned by tests/events/test_bus.py
            # TestWalPragmas.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit=33554432")
            conn.execute("PRAGMA wal_autocheckpoint=1000")
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

        # Get subscriber's cursor (last processed rowid).
        #
        # First-registration default: when no cursor row exists for this
        # subscriber_id, default to the CURRENT bus head — NOT zero. This
        # prevents a backlog flood the first time a new subscriber polls.
        # Surfaced twice on 2026-04-28: scribe-realtime (2D-1) and
        # scribe-action-telemetry (2D-2/2D-4) each emitted a burst of
        # historical narrations on initial registration before manual
        # cursor-skip mitigations took effect.
        #
        # If you genuinely want backfill, manually set last_rowid in
        # subscriber_cursors BEFORE the subscriber's first poll.
        row = conn.execute(
            "SELECT last_rowid FROM subscriber_cursors WHERE subscriber_id = ?",
            (subscriber_id,),
        ).fetchone()
        if row:
            last_rowid = row["last_rowid"]
        else:
            head = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
            last_rowid = head[0] if head and head[0] is not None else 0

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
        # Cap per-poll batch so a flood can be drained incrementally instead of
        # failing outright. Anything beyond this is picked up on the next poll.
        rows = conn.execute(
            f"SELECT rowid, * FROM events WHERE {where} ORDER BY rowid ASC LIMIT 2000",
            params,
        ).fetchall()

        events: List[Event] = []
        for r in rows:
            try:
                events.append(self._row_to_event(r))
            except ValueError as e:
                # Cross-version code skew: a producer using newer code can write
                # event_types the gateway hasn't loaded yet. Skip the row so the
                # subscriber poll loop doesn't crash; ack of any valid event in
                # this batch (or later batches) advances the cursor past it.
                logger.warning(
                    "subscribe(%s): skipping unparseable event %s (rowid=%s): %s",
                    subscriber_id, r["event_id"], r["rowid"], e,
                )
        return events

    def ack(self, subscriber_id: str, event_ids: List[str]) -> None:
        """Advance subscriber cursor past the given events.

        The cursor is set to the max rowid among the acked events.
        """
        if not event_ids:
            return
        # SQLite caps parameters at 32766 (SQLITE_LIMIT_VARIABLE_NUMBER). Chunk
        # conservatively so a backlog drain never raises "too many SQL variables".
        CHUNK = 500
        conn = self._get_conn()
        max_rowid: Optional[int] = None
        for start in range(0, len(event_ids), CHUNK):
            batch = event_ids[start:start + CHUNK]
            placeholders = ",".join("?" for _ in batch)
            row = conn.execute(
                f"SELECT MAX(rowid) as max_rowid FROM events WHERE event_id IN ({placeholders})",
                batch,
            ).fetchone()
            if row and row["max_rowid"] is not None:
                if max_rowid is None or row["max_rowid"] > max_rowid:
                    max_rowid = row["max_rowid"]
        if max_rowid is not None:
            self._execute(
                """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
                   VALUES (?, ?, datetime('now'))
                   ON CONFLICT(subscriber_id)
                   DO UPDATE SET last_rowid = excluded.last_rowid,
                                updated_at = excluded.updated_at""",
                (subscriber_id, max_rowid),
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

    def subscriber_lag(
        self,
        subscriber_id: str,
        event_types: Optional[List[EventType]] = None,
        min_priority: Optional[Priority] = None,
    ) -> int:
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
        # First-registration default: bus head, matching subscribe() above so
        # a new subscriber's lag reads 0 (consistent with what it will
        # actually process on first poll).
        if row:
            last_rowid = row["last_rowid"]
        else:
            head = conn.execute("SELECT MAX(rowid) FROM events").fetchone()
            last_rowid = head[0] if head and head[0] is not None else 0

        conditions = ["rowid > ?"]
        params: list = [last_rowid]

        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            conditions.append(f"event_type IN ({placeholders})")
            params.extend(et.type_string for et in event_types)

        if min_priority:
            valid = [p.label for p in Priority if p.level >= min_priority.level]
            placeholders = ",".join("?" for _ in valid)
            conditions.append(f"priority IN ({placeholders})")
            params.extend(valid)

        where = " AND ".join(conditions)
        row = conn.execute(
            f"SELECT COUNT(*) as n FROM events WHERE {where}",
            params,
        ).fetchone()
        return int(row["n"]) if row else 0

    def record_dead_letter(
        self, subscriber_id: str, event_id: str, error: str
    ) -> None:
        """Record that a subscriber's handler failed on a specific event.

        UPSERT semantics: on repeat failures for the same (event, subscriber),
        the row's ``attempts`` counter is incremented, ``error`` is overwritten
        with the latest message, and ``failed_at`` is refreshed. The original
        ``first_failed_at`` is preserved. Safe to call from subscriber
        exception handlers — failures in this call should NOT cascade, so the
        caller should wrap in its own try/except.
        """
        self._execute(
            """INSERT INTO dead_letters
                (event_id, subscriber_id, error, attempts, first_failed_at, failed_at)
               VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))
               ON CONFLICT(event_id, subscriber_id)
               DO UPDATE SET
                    attempts  = attempts + 1,
                    error     = excluded.error,
                    failed_at = excluded.failed_at""",
            (event_id, subscriber_id, error),
        )

    def get_dead_letters(
        self,
        subscriber_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """List dead-lettered events, newest first.

        Returns dicts with: event_id, subscriber_id, error, attempts,
        first_failed_at, failed_at, event_type, event_source, event_timestamp.
        The event_* fields come from a LEFT JOIN on events so that cleaned-up
        events (purged via cleanup()) still surface in the report.
        """
        conn = self._get_conn()
        conditions: List[str] = []
        params: List[Any] = []
        if subscriber_id:
            conditions.append("dl.subscriber_id = ?")
            params.append(subscriber_id)
        if since:
            conditions.append("dl.failed_at >= ?")
            params.append(since)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(int(limit))

        rows = conn.execute(
            f"""SELECT dl.event_id, dl.subscriber_id, dl.error, dl.attempts,
                       dl.first_failed_at, dl.failed_at,
                       e.event_type AS event_type, e.source AS event_source,
                       e.timestamp AS event_timestamp
                FROM dead_letters dl
                LEFT JOIN events e ON e.event_id = dl.event_id
                {where}
                ORDER BY dl.failed_at DESC, dl.event_id DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def is_handled(self, subscriber_id: str, event_id: str) -> bool:
        """Return True iff this subscriber has already processed this event.

        Part of the SR-101 at-least-once delivery contract: subscribers call
        ``is_handled`` before invoking ``handle()`` to detect redelivery (e.g.,
        after a crash between handle and ack) and avoid duplicate side effects.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM handled_events WHERE subscriber_id = ? AND event_id = ? LIMIT 1",
            (subscriber_id, event_id),
        ).fetchone()
        return row is not None

    def mark_handled(self, subscriber_id: str, event_id: str) -> None:
        """Record that a subscriber has finished processing an event.

        INSERT OR IGNORE so repeat marks (e.g., after a race on the UNIQUE
        PK) are silently tolerated — this is effectively idempotent.
        """
        self._execute(
            """INSERT OR IGNORE INTO handled_events (subscriber_id, event_id)
               VALUES (?, ?)""",
            (subscriber_id, event_id),
        )

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
