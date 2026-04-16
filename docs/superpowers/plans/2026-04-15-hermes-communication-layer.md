# Hermes Communication Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a SQLite-backed event bus with 6 notification subscribers that restore proactive communication from Hermes agents to the user via Telegram (primary) and WhatsApp (escalation), plus memory integration and audit logging.

**Architecture:** Event producers (CronEventEmitter, GatewayHealthMonitor, MailboxWatcher) emit typed events into a SQLite-backed EventBus. Six subscribers independently consume events: TelegramNotifier (forum topics), WhatsAppEscalator (quiet-hours-aware), DigestComposer (3x/day summaries), MemoryWriter (GBrain/MemPalace), AuditLogger (JSONL), and TelegramMirror (inter-agent shadow copies). All components run within the existing gateway process.

**Tech Stack:** Python 3.11+, SQLite (WAL mode), pytest, existing Hermes gateway/adapter infrastructure

**Spec:** `docs/superpowers/specs/2026-04-15-hermes-communication-layer-design.md`

**Source root:** `C:/Users/diego/Usersdiegohermes-agent-src/`

---

## File Structure

### New files to create:

```
events/
├── __init__.py                    # Package init, re-exports EventBus + EventType
├── schema.py                      # Event dataclass, EventType enum, Priority enum
├── bus.py                         # EventBus class (SQLite-backed)
├── producers/
│   ├── __init__.py                # Package init
│   ├── cron_emitter.py            # CronEventEmitter (hooks into tick/run_job)
│   ├── health_monitor.py          # GatewayHealthMonitor (WA + TG checks)
│   └── mailbox_watcher.py         # MailboxWatcher (polls inbox dirs)
└── subscribers/
    ├── __init__.py                 # Package init, SubscriberRegistry
    ├── base.py                     # BaseSubscriber abstract class
    ├── audit_logger.py             # AuditLogger subscriber
    ├── telegram_notifier.py        # TelegramNotifier subscriber
    ├── whatsapp_escalator.py       # WhatsAppEscalator subscriber
    ├── digest_composer.py          # DigestComposer subscriber
    ├── memory_writer.py            # MemoryWriter subscriber
    └── telegram_mirror.py          # TelegramMirror subscriber

scripts/
└── hermes_telegram_setup.py       # One-time Telegram group setup

tests/
├── events/
│   ├── __init__.py
│   ├── test_schema.py             # Event schema tests
│   ├── test_bus.py                # EventBus tests
│   ├── test_cron_emitter.py       # CronEventEmitter tests
│   ├── test_health_monitor.py     # GatewayHealthMonitor tests
│   ├── test_mailbox_watcher.py    # MailboxWatcher tests
│   ├── test_base_subscriber.py    # BaseSubscriber tests
│   ├── test_audit_logger.py       # AuditLogger tests
│   ├── test_telegram_notifier.py  # TelegramNotifier tests
│   ├── test_whatsapp_escalator.py # WhatsAppEscalator tests
│   ├── test_digest_composer.py    # DigestComposer tests
│   ├── test_memory_writer.py      # MemoryWriter tests
│   └── test_telegram_mirror.py    # TelegramMirror tests
```

### Existing files to modify:

```
cron/scheduler.py          # Hook CronEventEmitter into tick() and run_job()
cron/jobs.py               # Add consecutive_errors field tracking
gateway/run.py             # Wire EventBus + subscribers into gateway startup/shutdown
```

### Config files to create at runtime:

```
~/.hermes/events/              # Created by EventBus on first emit
~/.hermes/notifications/       # Created by WhatsAppEscalator
~/.hermes/telegram/            # Created by hermes_telegram_setup.py
```

---

## Task 1: Event Schema

**Files:**
- Create: `events/__init__.py`
- Create: `events/schema.py`
- Test: `tests/events/__init__.py`
- Test: `tests/events/test_schema.py`

- [ ] **Step 1: Create test directory and test file**

Create `tests/events/__init__.py` (empty) and `tests/events/test_schema.py`:

```python
"""Tests for events.schema — Event dataclass, EventType enum, Priority enum."""

import json
from datetime import datetime, timezone

from events.schema import Event, EventType, Priority


class TestPriority:
    def test_ordering(self):
        assert Priority.CRITICAL.level > Priority.HIGH.level
        assert Priority.HIGH.level > Priority.NORMAL.level
        assert Priority.NORMAL.level > Priority.LOW.level

    def test_from_string(self):
        assert Priority.from_string("critical") == Priority.CRITICAL
        assert Priority.from_string("HIGH") == Priority.HIGH
        assert Priority.from_string("Normal") == Priority.NORMAL
        assert Priority.from_string("low") == Priority.LOW
        assert Priority.from_string("unknown") == Priority.NORMAL  # fallback


class TestEventType:
    def test_all_catalog_types_exist(self):
        expected = [
            "cron_started", "cron_completed", "cron_failed", "cron_failed_consecutive",
            "job_discovered", "job_scored", "job_high_score", "job_vip_discovered",
            "tailor_completed", "application_ready", "application_submitted",
            "application_failed", "application_blocked",
            "stage_transition", "interview_signal", "offer_signal", "followup_due",
            "digest_generated", "gateway_health", "agent_error",
            "memory_consolidated", "skill_evolved", "mailbox_message",
        ]
        for name in expected:
            assert hasattr(EventType, name.upper()), f"Missing EventType.{name.upper()}"

    def test_default_priority(self):
        assert EventType.CRON_STARTED.default_priority == Priority.LOW
        assert EventType.CRON_FAILED.default_priority == Priority.HIGH
        assert EventType.INTERVIEW_SIGNAL.default_priority == Priority.CRITICAL
        assert EventType.JOB_SCORED.default_priority == Priority.NORMAL


class TestEvent:
    def test_create_minimal(self):
        event = Event.create(
            event_type=EventType.CRON_COMPLETED,
            source="scout",
            payload={"duration": 42.5},
        )
        assert event.event_type == EventType.CRON_COMPLETED
        assert event.source == "scout"
        assert event.priority == Priority.NORMAL  # default for cron_completed
        assert event.payload == {"duration": 42.5}
        assert event.event_id  # UUID generated
        assert event.timestamp  # Timestamp generated

    def test_create_with_overrides(self):
        event = Event.create(
            event_type=EventType.JOB_SCORED,
            source="matcher",
            payload={"score": 9.1},
            priority=Priority.HIGH,
            correlation_id="abc-123",
            job_id="ext-456",
            tags=["vip"],
        )
        assert event.priority == Priority.HIGH
        assert event.correlation_id == "abc-123"
        assert event.job_id == "ext-456"
        assert event.tags == ["vip"]

    def test_to_dict_roundtrip(self):
        event = Event.create(
            event_type=EventType.APPLICATION_SUBMITTED,
            source="applier",
            payload={"company": "Acme"},
            job_id="job-1",
            tags=["jobflow"],
        )
        d = event.to_dict()
        restored = Event.from_dict(d)
        assert restored.event_id == event.event_id
        assert restored.event_type == event.event_type
        assert restored.source == event.source
        assert restored.payload == event.payload
        assert restored.job_id == event.job_id
        assert restored.tags == event.tags

    def test_to_dict_is_json_serializable(self):
        event = Event.create(
            event_type=EventType.CRON_STARTED,
            source="scout",
            payload={"key": "value"},
        )
        json_str = json.dumps(event.to_dict())
        assert json_str  # No serialization error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_schema.py -v`

Expected: `ModuleNotFoundError: No module named 'events'`

- [ ] **Step 3: Create events package init**

Create `events/__init__.py`:

```python
"""Hermes Event Bus — event-driven notification and observability layer."""

from events.schema import Event, EventType, Priority

__all__ = ["Event", "EventType", "Priority"]
```

- [ ] **Step 4: Implement event schema**

Create `events/schema.py`:

```python
"""Event schema definitions for the Hermes Event Bus.

Defines the typed event envelope, event type catalog with default priorities,
and priority levels used for notification routing.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class Priority(Enum):
    """Event priority levels for notification routing.

    Each level has a numeric value for comparison and filtering.
    """

    CRITICAL = ("critical", 40)
    HIGH = ("high", 30)
    NORMAL = ("normal", 20)
    LOW = ("low", 10)

    def __init__(self, label: str, level: int):
        self.label = label
        self.level = level

    @classmethod
    def from_string(cls, value: str) -> "Priority":
        """Parse a priority string, falling back to NORMAL for unknown values."""
        lookup = {p.label: p for p in cls}
        return lookup.get(value.lower(), cls.NORMAL)


class EventType(Enum):
    """Catalog of all event types emitted by the Hermes Event Bus.

    Each member is a tuple of (event_type_string, default_priority).
    """

    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW)
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL)
    CRON_FAILED = ("cron_failed", Priority.HIGH)
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL)

    # Job discovery & scoring
    JOB_DISCOVERED = ("job_discovered", Priority.NORMAL)
    JOB_SCORED = ("job_scored", Priority.NORMAL)
    JOB_HIGH_SCORE = ("job_high_score", Priority.HIGH)
    JOB_VIP_DISCOVERED = ("job_vip_discovered", Priority.HIGH)

    # Tailoring & applications
    TAILOR_COMPLETED = ("tailor_completed", Priority.NORMAL)
    APPLICATION_READY = ("application_ready", Priority.HIGH)
    APPLICATION_SUBMITTED = ("application_submitted", Priority.HIGH)
    APPLICATION_FAILED = ("application_failed", Priority.CRITICAL)
    APPLICATION_BLOCKED = ("application_blocked", Priority.CRITICAL)

    # Pipeline tracking
    STAGE_TRANSITION = ("stage_transition", Priority.NORMAL)
    INTERVIEW_SIGNAL = ("interview_signal", Priority.CRITICAL)
    OFFER_SIGNAL = ("offer_signal", Priority.CRITICAL)
    FOLLOWUP_DUE = ("followup_due", Priority.HIGH)

    # System
    DIGEST_GENERATED = ("digest_generated", Priority.LOW)
    GATEWAY_HEALTH = ("gateway_health", Priority.HIGH)
    AGENT_ERROR = ("agent_error", Priority.HIGH)
    MEMORY_CONSOLIDATED = ("memory_consolidated", Priority.LOW)
    SKILL_EVOLVED = ("skill_evolved", Priority.LOW)
    MAILBOX_MESSAGE = ("mailbox_message", Priority.LOW)

    def __init__(self, type_string: str, default_priority: Priority):
        self.type_string = type_string
        self.default_priority = default_priority

    @classmethod
    def from_string(cls, value: str) -> Optional["EventType"]:
        """Look up an EventType by its string name. Returns None if not found."""
        lookup = {et.type_string: et for et in cls}
        return lookup.get(value.lower())


@dataclass
class Event:
    """A single typed event in the Hermes Event Bus.

    Events are the universal unit of communication between producers
    (cron jobs, agents, health monitors) and subscribers (Telegram notifier,
    WhatsApp escalator, memory writer, etc.).
    """

    event_id: str
    event_type: EventType
    source: str
    timestamp: str  # ISO8601 UTC
    priority: Priority
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    job_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        event_type: EventType,
        source: str,
        payload: Dict[str, Any],
        priority: Optional[Priority] = None,
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> "Event":
        """Create a new event with auto-generated ID and timestamp."""
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            priority=priority or event_type.default_priority,
            payload=payload,
            correlation_id=correlation_id,
            job_id=job_id,
            tags=tags or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.type_string,
            "source": self.source,
            "timestamp": self.timestamp,
            "priority": self.priority.label,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "job_id": self.job_id,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Deserialize from a dict (e.g., from SQLite JSON or audit log)."""
        event_type = EventType.from_string(data["event_type"])
        if event_type is None:
            raise ValueError(f"Unknown event type: {data['event_type']}")
        return cls(
            event_id=data["event_id"],
            event_type=event_type,
            source=data["source"],
            timestamp=data["timestamp"],
            priority=Priority.from_string(data["priority"]),
            payload=data.get("payload", {}),
            correlation_id=data.get("correlation_id"),
            job_id=data.get("job_id"),
            tags=data.get("tags", []),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_schema.py -v`

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/__init__.py events/schema.py tests/events/__init__.py tests/events/test_schema.py
git commit -m "feat(events): add event schema — EventType catalog, Priority levels, Event dataclass"
```

---

## Task 2: EventBus Core

**Files:**
- Create: `events/bus.py`
- Test: `tests/events/test_bus.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_bus.py`:

```python
"""Tests for events.bus — SQLite-backed EventBus."""

import os
import threading
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority


@pytest.fixture
def bus(tmp_path):
    """Create an EventBus backed by a temp SQLite database."""
    db_path = tmp_path / "events" / "event_bus.db"
    return EventBus(db_path=db_path)


class TestEmit:
    def test_emit_returns_event_id(self, bus):
        event_id = bus.emit(
            event_type=EventType.CRON_COMPLETED,
            source="scout",
            payload={"duration": 10.5},
        )
        assert event_id
        assert isinstance(event_id, str)

    def test_emit_creates_db_and_dirs(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        assert bus.db_path.exists()
        assert bus.db_path.parent.exists()

    def test_emit_with_all_fields(self, bus):
        event_id = bus.emit(
            event_type=EventType.JOB_HIGH_SCORE,
            source="matcher",
            payload={"score": 9.1, "company": "Acme"},
            priority=Priority.CRITICAL,
            correlation_id="corr-1",
            job_id="job-ext-1",
            tags=["vip", "finance"],
        )
        events = bus.query(event_type=EventType.JOB_HIGH_SCORE)
        assert len(events) == 1
        assert events[0].event_id == event_id
        assert events[0].priority == Priority.CRITICAL
        assert events[0].correlation_id == "corr-1"
        assert events[0].job_id == "job-ext-1"
        assert events[0].tags == ["vip", "finance"]


class TestSubscribe:
    def test_subscribe_returns_new_events(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"a": 1})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {"b": 2})

        events = bus.subscribe("test-sub")
        assert len(events) == 2
        assert events[0].payload == {"a": 1}
        assert events[1].payload == {"b": 2}

    def test_subscribe_with_type_filter(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "VP Finance"})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        events = bus.subscribe("test-sub", event_types=[EventType.JOB_DISCOVERED])
        assert len(events) == 1
        assert events[0].event_type == EventType.JOB_DISCOVERED

    def test_subscribe_with_priority_filter(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})  # LOW
        bus.emit(EventType.CRON_FAILED, "scout", {})  # HIGH
        bus.emit(EventType.INTERVIEW_SIGNAL, "tracker", {})  # CRITICAL

        events = bus.subscribe("test-sub", min_priority=Priority.HIGH)
        assert len(events) == 2

    def test_subscribe_cursor_advances(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"batch": 1})

        events1 = bus.subscribe("test-sub")
        assert len(events1) == 1
        bus.ack("test-sub", [e.event_id for e in events1])

        bus.emit(EventType.CRON_COMPLETED, "matcher", {"batch": 2})

        events2 = bus.subscribe("test-sub")
        assert len(events2) == 1
        assert events2[0].payload == {"batch": 2}

    def test_independent_subscribers(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {"x": 1})

        events_a = bus.subscribe("sub-a")
        events_b = bus.subscribe("sub-b")
        assert len(events_a) == 1
        assert len(events_b) == 1

        bus.ack("sub-a", [e.event_id for e in events_a])

        bus.emit(EventType.CRON_COMPLETED, "matcher", {"x": 2})

        events_a2 = bus.subscribe("sub-a")
        events_b2 = bus.subscribe("sub-b")
        assert len(events_a2) == 1  # only new event
        assert len(events_b2) == 2  # both events (never acked)


class TestQuery:
    def test_query_by_type(self, bus):
        bus.emit(EventType.CRON_STARTED, "scout", {})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        results = bus.query(event_type=EventType.JOB_DISCOVERED)
        assert len(results) == 1

    def test_query_by_source(self, bus):
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {})

        results = bus.query(source="matcher")
        assert len(results) == 1
        assert results[0].source == "matcher"

    def test_query_by_correlation_id(self, bus):
        bus.emit(EventType.JOB_DISCOVERED, "scout", {}, correlation_id="flow-1")
        bus.emit(EventType.JOB_SCORED, "matcher", {}, correlation_id="flow-1")
        bus.emit(EventType.JOB_DISCOVERED, "scout", {}, correlation_id="flow-2")

        results = bus.query(correlation_id="flow-1")
        assert len(results) == 2


class TestCleanup:
    def test_cleanup_removes_old_events(self, bus):
        # Emit an event, then manually backdate it
        event_id = bus.emit(EventType.CRON_STARTED, "scout", {})
        bus._execute(
            "UPDATE events SET created_at = datetime('now', '-31 days') WHERE event_id = ?",
            (event_id,),
        )
        bus.emit(EventType.CRON_STARTED, "scout", {})  # recent event

        removed = bus.cleanup(retention_days=30)
        assert removed == 1

        remaining = bus.query()
        assert len(remaining) == 1


class TestThreadSafety:
    def test_concurrent_emits(self, bus):
        errors = []

        def emit_events(source: str):
            try:
                for i in range(20):
                    bus.emit(EventType.CRON_COMPLETED, source, {"i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=emit_events, args=(f"agent-{n}",)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        all_events = bus.query()
        assert len(all_events) == 80  # 4 threads * 20 events
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_bus.py -v`

Expected: `ModuleNotFoundError: No module named 'events.bus'`

- [ ] **Step 3: Implement EventBus**

Create `events/bus.py`:

```python
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
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_type_ts
    ON events (event_type, created_at);
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
            from hermes_constants import get_hermes_home
            db_path = get_hermes_home() / "events" / "event_bus.db"
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
        """Create tables if they don't exist."""
        conn = self._get_conn()
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

        Does NOT advance the cursor — call ack() after processing.
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
```

- [ ] **Step 4: Update events/__init__.py to export EventBus**

```python
"""Hermes Event Bus — event-driven notification and observability layer."""

from events.schema import Event, EventType, Priority
from events.bus import EventBus

__all__ = ["Event", "EventType", "Priority", "EventBus"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_bus.py -v`

Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/bus.py events/__init__.py tests/events/test_bus.py
git commit -m "feat(events): add SQLite-backed EventBus with emit/subscribe/ack/query"
```

---

## Task 3: BaseSubscriber and SubscriberRegistry

**Files:**
- Create: `events/subscribers/__init__.py`
- Create: `events/subscribers/base.py`
- Test: `tests/events/test_base_subscriber.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_base_subscriber.py`:

```python
"""Tests for events.subscribers.base — BaseSubscriber and SubscriberRegistry."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.subscribers.base import BaseSubscriber, SubscriberRegistry


class StubSubscriber(BaseSubscriber):
    """Concrete subscriber for testing."""

    subscriber_id = "stub"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.processed = []

    def handle(self, event):
        self.processed.append(event)


class FilteredSubscriber(BaseSubscriber):
    subscriber_id = "filtered"
    poll_interval_seconds = 10
    event_types = [EventType.JOB_HIGH_SCORE, EventType.INTERVIEW_SIGNAL]
    min_priority = Priority.HIGH

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.processed = []

    def handle(self, event):
        self.processed.append(event)


class TestBaseSubscriber:
    def test_poll_processes_events(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        bus.emit(EventType.CRON_COMPLETED, "scout", {"a": 1})
        bus.emit(EventType.CRON_STARTED, "matcher", {"b": 2})

        processed = sub.poll()
        assert processed == 2
        assert len(sub.processed) == 2

    def test_poll_advances_cursor(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        sub.poll()
        assert len(sub.processed) == 1

        bus.emit(EventType.CRON_COMPLETED, "matcher", {})
        sub.poll()
        assert len(sub.processed) == 2  # only the new one

    def test_filtered_subscriber(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FilteredSubscriber(bus)

        bus.emit(EventType.CRON_STARTED, "scout", {})        # LOW — filtered
        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {})     # HIGH — passes
        bus.emit(EventType.CRON_COMPLETED, "scout", {})       # NORMAL — filtered
        bus.emit(EventType.INTERVIEW_SIGNAL, "tracker", {})   # CRITICAL — passes

        processed = sub.poll()
        assert processed == 2
        assert sub.processed[0].event_type == EventType.JOB_HIGH_SCORE
        assert sub.processed[1].event_type == EventType.INTERVIEW_SIGNAL

    def test_handle_error_does_not_stop_processing(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")

        class FailingSubscriber(BaseSubscriber):
            subscriber_id = "failing"
            poll_interval_seconds = 5

            def __init__(self, bus):
                super().__init__(bus)
                self.calls = 0

            def handle(self, event):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("boom")

        sub = FailingSubscriber(bus)
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {})

        processed = sub.poll()
        assert processed == 2  # both processed despite first error
        assert sub.calls == 2


class TestSubscriberRegistry:
    def test_register_and_list(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        sub = StubSubscriber(bus)
        registry.register(sub)

        assert len(registry.subscribers) == 1
        assert registry.subscribers[0] is sub

    def test_poll_all(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        sub1 = StubSubscriber(bus)
        sub1.subscriber_id = "stub-1"
        sub2 = StubSubscriber(bus)
        sub2.subscriber_id = "stub-2"

        registry.register(sub1)
        registry.register(sub2)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        results = registry.poll_all()
        assert results == {"stub-1": 1, "stub-2": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_base_subscriber.py -v`

Expected: `ModuleNotFoundError: No module named 'events.subscribers'`

- [ ] **Step 3: Implement BaseSubscriber and SubscriberRegistry**

Create `events/subscribers/__init__.py`:

```python
"""Notification subscribers for the Hermes Event Bus."""

from events.subscribers.base import BaseSubscriber, SubscriberRegistry

__all__ = ["BaseSubscriber", "SubscriberRegistry"]
```

Create `events/subscribers/base.py`:

```python
"""Base subscriber class and registry for the Hermes Event Bus.

Subscribers independently consume events from the bus via poll().
Each subscriber tracks its own cursor so multiple subscribers
can process the same events without interference.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)


class BaseSubscriber(ABC):
    """Abstract base class for event bus subscribers.

    Subclasses must define:
      - subscriber_id: unique string identifier
      - poll_interval_seconds: how often to poll (used by the runner)
      - handle(event): process a single event

    Optionally override:
      - event_types: list of EventType to filter (None = all)
      - min_priority: minimum Priority to receive (None = all)
    """

    subscriber_id: str = ""
    poll_interval_seconds: int = 5
    event_types: Optional[List[EventType]] = None
    min_priority: Optional[Priority] = None

    def __init__(self, bus: EventBus):
        self.bus = bus

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Process a single event.  Exceptions are caught and logged."""

    def poll(self) -> int:
        """Fetch and process new events since last cursor.  Returns count processed."""
        events = self.bus.subscribe(
            subscriber_id=self.subscriber_id,
            event_types=self.event_types,
            min_priority=self.min_priority,
        )
        if not events:
            return 0

        processed_ids = []
        for event in events:
            try:
                self.handle(event)
            except Exception:
                logger.exception(
                    "Subscriber %s failed to handle event %s (%s)",
                    self.subscriber_id, event.event_id, event.event_type.type_string,
                )
            processed_ids.append(event.event_id)

        self.bus.ack(self.subscriber_id, processed_ids)
        return len(events)

    def startup(self) -> None:
        """Called once when the subscriber is registered.  Override for init logic."""

    def shutdown(self) -> None:
        """Called once on gateway shutdown.  Override for cleanup logic."""


class SubscriberRegistry:
    """Manages a set of subscribers and coordinates polling."""

    def __init__(self):
        self.subscribers: List[BaseSubscriber] = []

    def register(self, subscriber: BaseSubscriber) -> None:
        """Add a subscriber to the registry."""
        self.subscribers.append(subscriber)
        logger.info("Registered subscriber: %s", subscriber.subscriber_id)

    def poll_all(self) -> Dict[str, int]:
        """Poll all subscribers and return {subscriber_id: events_processed}."""
        results = {}
        for sub in self.subscribers:
            try:
                count = sub.poll()
                results[sub.subscriber_id] = count
            except Exception:
                logger.exception("Failed to poll subscriber %s", sub.subscriber_id)
                results[sub.subscriber_id] = 0
        return results

    def startup_all(self) -> None:
        """Call startup() on all subscribers."""
        for sub in self.subscribers:
            try:
                sub.startup()
            except Exception:
                logger.exception("Subscriber %s startup failed", sub.subscriber_id)

    def shutdown_all(self) -> None:
        """Call shutdown() on all subscribers."""
        for sub in self.subscribers:
            try:
                sub.shutdown()
            except Exception:
                logger.exception("Subscriber %s shutdown failed", sub.subscriber_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_base_subscriber.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/__init__.py events/subscribers/base.py tests/events/test_base_subscriber.py
git commit -m "feat(events): add BaseSubscriber abstract class and SubscriberRegistry"
```

---

## Task 4: AuditLogger Subscriber

The simplest subscriber — validates the pattern works end-to-end before building complex ones.

**Files:**
- Create: `events/subscribers/audit_logger.py`
- Test: `tests/events/test_audit_logger.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_audit_logger.py`:

```python
"""Tests for events.subscribers.audit_logger — JSONL audit trail."""

import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.audit_logger import AuditLogger


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "events" / "audit.jsonl"


class TestAuditLogger:
    def test_logs_events_as_jsonl(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        bus.emit(EventType.CRON_COMPLETED, "scout", {"jobs": 5})
        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {"score": 9.1})

        logger.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2

        entry1 = json.loads(lines[0])
        assert entry1["event_type"] == "cron_completed"
        assert entry1["source"] == "scout"
        assert entry1["payload"] == {"jobs": 5}

        entry2 = json.loads(lines[1])
        assert entry2["event_type"] == "job_high_score"

    def test_appends_to_existing_file(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        logger.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_parent_dirs(self, bus, tmp_path):
        deep_path = tmp_path / "a" / "b" / "c" / "audit.jsonl"
        logger = AuditLogger(bus, audit_path=deep_path)

        bus.emit(EventType.CRON_STARTED, "scout", {})
        logger.poll()

        assert deep_path.exists()

    def test_handles_no_events(self, bus, audit_path):
        logger = AuditLogger(bus, audit_path=audit_path)
        count = logger.poll()
        assert count == 0
        assert not audit_path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_audit_logger.py -v`

Expected: `ModuleNotFoundError: No module named 'events.subscribers.audit_logger'`

- [ ] **Step 3: Implement AuditLogger**

Create `events/subscribers/audit_logger.py`:

```python
"""AuditLogger subscriber — append-only JSONL event trail.

Records every event for debugging and replay.  Rotated weekly by
an external cleanup job, retained for 90 days.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from events.bus import EventBus
from events.schema import Event
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class AuditLogger(BaseSubscriber):
    subscriber_id = "audit-logger"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus, audit_path: Optional[Path] = None):
        super().__init__(bus)
        if audit_path is None:
            from hermes_constants import get_hermes_home
            audit_path = get_hermes_home() / "events" / "audit.jsonl"
        self.audit_path = Path(audit_path)

    def handle(self, event: Event) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_audit_logger.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/audit_logger.py tests/events/test_audit_logger.py
git commit -m "feat(events): add AuditLogger subscriber — JSONL event trail"
```

---

## Task 5: CronEventEmitter Producer

**Files:**
- Create: `events/producers/__init__.py`
- Create: `events/producers/cron_emitter.py`
- Modify: `cron/scheduler.py` (hook into `tick()`)
- Modify: `cron/jobs.py` (add `consecutive_errors` tracking)
- Test: `tests/events/test_cron_emitter.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_cron_emitter.py`:

```python
"""Tests for events.producers.cron_emitter — CronEventEmitter."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.cron_emitter import CronEventEmitter


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def emitter(bus):
    return CronEventEmitter(bus)


class TestCronLifecycle:
    def test_emit_started(self, emitter, bus):
        emitter.on_job_started("job-1", "jobflow-scout", "0 8,13,18 * * *")

        events = bus.query(event_type=EventType.CRON_STARTED)
        assert len(events) == 1
        assert events[0].source == "jobflow-scout"
        assert events[0].priority == Priority.LOW
        assert events[0].payload["job_id"] == "job-1"
        assert events[0].payload["schedule"] == "0 8,13,18 * * *"

    def test_emit_completed(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-1",
            job_name="jobflow-scout",
            success=True,
            duration=42.5,
            output_summary="Found 8 new jobs",
        )

        events = bus.query(event_type=EventType.CRON_COMPLETED)
        assert len(events) == 1
        assert events[0].payload["duration"] == 42.5
        assert events[0].payload["output_summary"] == "Found 8 new jobs"

    def test_emit_failed(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-1",
            job_name="jobflow-scout",
            success=False,
            duration=10.0,
            error="Connection timeout",
        )

        events = bus.query(event_type=EventType.CRON_FAILED)
        assert len(events) == 1
        assert events[0].priority == Priority.HIGH
        assert events[0].payload["error"] == "Connection timeout"

    def test_emit_consecutive_failure(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-1",
            job_name="jobflow-scout",
            success=False,
            duration=5.0,
            error="fail",
            consecutive_errors=3,
        )

        failed = bus.query(event_type=EventType.CRON_FAILED)
        consecutive = bus.query(event_type=EventType.CRON_FAILED_CONSECUTIVE)
        assert len(failed) == 1
        assert len(consecutive) == 1
        assert consecutive[0].priority == Priority.CRITICAL
        assert consecutive[0].payload["consecutive_errors"] == 3

    def test_no_consecutive_event_below_threshold(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-1",
            job_name="jobflow-scout",
            success=False,
            duration=5.0,
            error="fail",
            consecutive_errors=2,
        )

        consecutive = bus.query(event_type=EventType.CRON_FAILED_CONSECUTIVE)
        assert len(consecutive) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_cron_emitter.py -v`

Expected: `ModuleNotFoundError: No module named 'events.producers'`

- [ ] **Step 3: Implement CronEventEmitter**

Create `events/producers/__init__.py`:

```python
"""Event producers for the Hermes Event Bus."""
```

Create `events/producers/cron_emitter.py`:

```python
"""CronEventEmitter — emits lifecycle events from the cron execution pipeline.

Hooks into the cron scheduler's tick()/run_job() cycle to emit:
  - cron_started: before job execution
  - cron_completed: after successful execution
  - cron_failed: after failed execution
  - cron_failed_consecutive: when consecutive failures reach threshold
"""

import logging
from typing import Optional

from events.bus import EventBus
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)

CONSECUTIVE_FAILURE_THRESHOLD = 3


class CronEventEmitter:
    """Emits cron lifecycle events into the EventBus."""

    def __init__(self, bus: EventBus):
        self.bus = bus

    def on_job_started(
        self,
        job_id: str,
        job_name: str,
        schedule: str,
    ) -> str:
        """Emit cron_started event before job execution."""
        return self.bus.emit(
            event_type=EventType.CRON_STARTED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "schedule": schedule,
            },
        )

    def on_job_completed(
        self,
        job_id: str,
        job_name: str,
        success: bool,
        duration: float,
        output_summary: Optional[str] = None,
        error: Optional[str] = None,
        consecutive_errors: int = 0,
    ) -> str:
        """Emit cron_completed or cron_failed event after job execution.

        If consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD, also emits
        cron_failed_consecutive as a separate critical event.
        """
        if success:
            event_id = self.bus.emit(
                event_type=EventType.CRON_COMPLETED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "output_summary": output_summary or "",
                },
            )
        else:
            event_id = self.bus.emit(
                event_type=EventType.CRON_FAILED,
                source=job_name,
                payload={
                    "job_id": job_id,
                    "job_name": job_name,
                    "duration": duration,
                    "error": error or "Unknown error",
                    "consecutive_errors": consecutive_errors,
                },
            )

            if consecutive_errors >= CONSECUTIVE_FAILURE_THRESHOLD:
                self.bus.emit(
                    event_type=EventType.CRON_FAILED_CONSECUTIVE,
                    source=job_name,
                    payload={
                        "job_id": job_id,
                        "job_name": job_name,
                        "consecutive_errors": consecutive_errors,
                        "error": error or "Unknown error",
                    },
                )

        return event_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_cron_emitter.py -v`

Expected: All tests PASS

- [ ] **Step 5: Add consecutive_errors tracking to cron/jobs.py**

In `cron/jobs.py`, modify `mark_job_run()` (around line 586) to track consecutive errors. Add after `job["last_error"] = error if not success else None` (line 603):

```python
            # Track consecutive errors for alerting
            if success:
                job["consecutive_errors"] = 0
            else:
                job["consecutive_errors"] = job.get("consecutive_errors", 0) + 1
```

- [ ] **Step 6: Hook CronEventEmitter into cron/scheduler.py tick()**

In `cron/scheduler.py`, add at the top of the file (after existing imports):

```python
# Event bus integration (lazy-loaded to avoid circular imports)
_event_emitter = None

def _get_event_emitter():
    """Lazy-load the CronEventEmitter to avoid import-time side effects."""
    global _event_emitter
    if _event_emitter is None:
        try:
            from events.bus import EventBus
            from events.producers.cron_emitter import CronEventEmitter
            _event_emitter = CronEventEmitter(EventBus())
        except Exception as e:
            logger.debug("Event bus not available: %s", e)
            _event_emitter = False  # sentinel: don't retry
    return _event_emitter if _event_emitter else None
```

Then in `tick()`, wrap the job execution block (around lines 948-984). Replace:

```python
        for job in due_jobs:
            try:
                # For recurring jobs (cron/interval), advance next_run_at to the
                # next future occurrence BEFORE execution.  This way, if the
                # process crashes mid-run, the job won't re-fire on restart.
                # One-shot jobs are left alone so they can retry on restart.
                advance_next_run(job["id"])

                success, output, final_response, error = run_job(job)
```

With:

```python
        for job in due_jobs:
            try:
                advance_next_run(job["id"])

                # Emit cron_started event
                emitter = _get_event_emitter()
                if emitter:
                    try:
                        emitter.on_job_started(
                            job_id=job["id"],
                            job_name=job.get("name", job["id"]),
                            schedule=job.get("schedule_display", ""),
                        )
                    except Exception as ee:
                        logger.debug("Event emit failed: %s", ee)

                import time as _time
                _job_start = _time.monotonic()
                success, output, final_response, error = run_job(job)
                _job_duration = _time.monotonic() - _job_start
```

And after `mark_job_run(job["id"], success, error, delivery_error=delivery_error)` (line 979), add:

```python
                # Emit completion/failure event
                if emitter:
                    try:
                        from cron.jobs import load_jobs
                        current_job = next(
                            (j for j in load_jobs() if j["id"] == job["id"]), None
                        )
                        consecutive = current_job.get("consecutive_errors", 0) if current_job else 0
                        summary = (final_response or "")[:500] if success else None
                        emitter.on_job_completed(
                            job_id=job["id"],
                            job_name=job.get("name", job["id"]),
                            success=success,
                            duration=round(_job_duration, 1),
                            output_summary=summary,
                            error=error,
                            consecutive_errors=consecutive,
                        )
                    except Exception as ee:
                        logger.debug("Event emit failed: %s", ee)
```

- [ ] **Step 7: Run full test suite to verify no regressions**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/ tests/cron/ -v --timeout=30`

Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/producers/__init__.py events/producers/cron_emitter.py \
        tests/events/test_cron_emitter.py cron/scheduler.py cron/jobs.py
git commit -m "feat(events): add CronEventEmitter and hook into cron pipeline"
```

---

## Task 6: GatewayHealthMonitor Producer

**Files:**
- Create: `events/producers/health_monitor.py`
- Test: `tests/events/test_health_monitor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_health_monitor.py`:

```python
"""Tests for events.producers.health_monitor — GatewayHealthMonitor."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.health_monitor import GatewayHealthMonitor


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


class TestHealthMonitor:
    def test_emits_on_state_change_down(self, bus):
        monitor = GatewayHealthMonitor(bus)

        # Initially unknown → transition to down
        monitor.report_health("whatsapp", healthy=False, detail="Bridge unreachable")

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 1
        assert events[0].payload["platform"] == "whatsapp"
        assert events[0].payload["status"] == "down"
        assert events[0].payload["detail"] == "Bridge unreachable"

    def test_no_event_on_same_state(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("telegram", healthy=True)
        monitor.report_health("telegram", healthy=True)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        # First report transitions from unknown→up, second is same state
        assert len(events) == 1

    def test_emits_on_recovery(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("whatsapp", healthy=False)
        monitor.report_health("whatsapp", healthy=True)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 2
        assert events[0].payload["status"] == "down"
        assert events[1].payload["status"] == "up"

    def test_tracks_platforms_independently(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("whatsapp", healthy=True)
        monitor.report_health("telegram", healthy=False)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_health_monitor.py -v`

Expected: `ModuleNotFoundError: No module named 'events.producers.health_monitor'`

- [ ] **Step 3: Implement GatewayHealthMonitor**

Create `events/producers/health_monitor.py`:

```python
"""GatewayHealthMonitor — emits events on platform health state changes.

Only emits gateway_health events on transitions (up→down or down→up),
not on every check cycle.  Tracks each platform independently.
"""

import logging
from typing import Dict, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)


class GatewayHealthMonitor:
    """Tracks platform health and emits events on state transitions."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._last_state: Dict[str, bool] = {}  # platform → healthy

    def report_health(
        self,
        platform: str,
        healthy: bool,
        detail: Optional[str] = None,
    ) -> Optional[str]:
        """Report a platform's health.  Emits event only on state change.

        Returns event_id if an event was emitted, None otherwise.
        """
        prev = self._last_state.get(platform)
        self._last_state[platform] = healthy

        if prev == healthy:
            return None  # No state change

        status = "up" if healthy else "down"
        logger.info("Gateway health: %s → %s", platform, status)

        return self.bus.emit(
            event_type=EventType.GATEWAY_HEALTH,
            source="system",
            payload={
                "platform": platform,
                "status": status,
                "detail": detail or "",
            },
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_health_monitor.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/producers/health_monitor.py tests/events/test_health_monitor.py
git commit -m "feat(events): add GatewayHealthMonitor — state-change-only health events"
```

---

## Task 7: MailboxWatcher Producer

**Files:**
- Create: `events/producers/mailbox_watcher.py`
- Test: `tests/events/test_mailbox_watcher.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_mailbox_watcher.py`:

```python
"""Tests for events.producers.mailbox_watcher — MailboxWatcher."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.producers.mailbox_watcher import MailboxWatcher

MIRRORED_TYPES = {"SCORE_RESULT", "TAILOR_REQUEST", "SUBMIT_REQUEST", "SCOUT_DISCOVERY"}


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def mailbox_root(tmp_path):
    root = tmp_path / "mailbox"
    for profile in ("main", "scout", "matcher", "tracker"):
        (root / profile / "inbox").mkdir(parents=True)
    return root


def _write_message(inbox: Path, msg_type: str, sender: str, payload: dict) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{ts}_{msg_type}_{sender}.json"
    msg = {"type": msg_type, "from": sender, "to": inbox.parent.name, "payload": payload}
    path = inbox / filename
    path.write_text(json.dumps(msg), encoding="utf-8")
    return path


class TestMailboxWatcher:
    def test_detects_new_messages(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        _write_message(
            mailbox_root / "tracker" / "inbox",
            "SCOUT_DISCOVERY", "scout",
            {"jobs": [{"title": "VP Finance"}]},
        )

        count = watcher.scan()
        assert count == 1

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1
        assert events[0].payload["message_type"] == "SCOUT_DISCOVERY"
        assert events[0].payload["from"] == "scout"
        assert events[0].payload["to"] == "tracker"

    def test_skips_already_seen(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        _write_message(mailbox_root / "main" / "inbox", "SCORE_RESULT", "matcher", {})

        assert watcher.scan() == 1
        assert watcher.scan() == 0  # same file, already seen

    def test_filters_non_protocol_files(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        # Write a file that doesn't match the naming convention
        (mailbox_root / "main" / "inbox" / "random.txt").write_text("not a message")

        assert watcher.scan() == 0

    def test_filters_sweeper_files(self, bus, mailbox_root):
        watcher = MailboxWatcher(bus, mailbox_root=mailbox_root)

        # Sweeper operations use non-standard types
        _write_message(mailbox_root / "main" / "inbox", "SWEEP_COMPLETE", "system", {})

        assert watcher.scan() == 0

    def test_persists_watermark(self, bus, mailbox_root):
        watcher1 = MailboxWatcher(bus, mailbox_root=mailbox_root)
        _write_message(mailbox_root / "main" / "inbox", "SCORE_RESULT", "matcher", {})
        watcher1.scan()

        # New watcher instance loads watermark from disk
        watcher2 = MailboxWatcher(bus, mailbox_root=mailbox_root)
        _write_message(mailbox_root / "main" / "inbox", "TAILOR_REQUEST", "main", {})

        assert watcher2.scan() == 1  # only the new message
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_mailbox_watcher.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement MailboxWatcher**

Create `events/producers/mailbox_watcher.py`:

```python
"""MailboxWatcher — polls inter-agent mailbox for new messages and emits events.

Scans ~/.hermes/mailbox/*/inbox/ for new JSON files matching the protocol
naming convention.  Substantive messages are emitted as mailbox_message events.
Tracks seen files via a watermark file to avoid re-processing.
"""

import json
import logging
from pathlib import Path
from typing import Optional, Set

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

# Message types worth mirroring (from protocol.md)
MIRRORED_MESSAGE_TYPES = {
    "SCOUT_DISCOVERY", "SCORE_REQUEST", "SCORE_RESULT", "SCORE_BATCH_SUMMARY",
    "TAILOR_REQUEST", "TAILOR_COMPLETE", "TAILOR_REVISION",
    "TAILOR_MODULE_REQUEST", "TAILOR_MODULE_COMPLETE",
    "SUBMIT_REQUEST", "DRY_RUN_COMPLETE", "SUBMIT_CONFIRM", "BLOCKED_QUESTION",
    "PIPELINE_UPDATE", "STATUS_REQUEST", "STATUS_RESPONSE", "FOLLOWUP_ALERT",
    "NOTIFICATION", "HIGH_SCORE_ALERT",
    "VIP_DISCOVERY", "VIP_PROMOTE", "VIP_SCAN_REQUEST",
    "KB_QUERY", "KB_RESPONSE", "ERROR",
}


class MailboxWatcher:
    """Polls inter-agent mailbox directories for new protocol messages."""

    def __init__(
        self,
        bus: EventBus,
        mailbox_root: Optional[Path] = None,
    ):
        self.bus = bus
        if mailbox_root is None:
            from hermes_constants import get_hermes_home
            mailbox_root = get_hermes_home() / "mailbox"
        self.mailbox_root = Path(mailbox_root)
        self._watermark_path = self.mailbox_root / ".event_watermark.json"
        self._seen: Set[str] = self._load_watermark()

    def _load_watermark(self) -> Set[str]:
        """Load the set of already-seen file paths from disk."""
        if self._watermark_path.exists():
            try:
                data = json.loads(self._watermark_path.read_text(encoding="utf-8"))
                return set(data.get("seen", []))
            except (json.JSONDecodeError, KeyError):
                return set()
        return set()

    def _save_watermark(self) -> None:
        """Persist the seen set to disk."""
        self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep only the last 2000 entries to prevent unbounded growth
        trimmed = sorted(self._seen)[-2000:]
        self._seen = set(trimmed)
        self._watermark_path.write_text(
            json.dumps({"seen": trimmed}),
            encoding="utf-8",
        )

    def scan(self) -> int:
        """Scan all inboxes for new messages.  Returns count of events emitted."""
        if not self.mailbox_root.exists():
            return 0

        count = 0
        for profile_dir in self.mailbox_root.iterdir():
            if not profile_dir.is_dir():
                continue
            inbox = profile_dir / "inbox"
            if not inbox.exists():
                continue

            for msg_file in inbox.iterdir():
                if not msg_file.is_file() or not msg_file.suffix == ".json":
                    continue

                file_key = str(msg_file.relative_to(self.mailbox_root))
                if file_key in self._seen:
                    continue

                self._seen.add(file_key)

                if not self._is_protocol_message(msg_file.name):
                    continue

                try:
                    msg = json.loads(msg_file.read_text(encoding="utf-8"))
                    msg_type = msg.get("type", "")
                    if msg_type not in MIRRORED_MESSAGE_TYPES:
                        continue

                    self.bus.emit(
                        event_type=EventType.MAILBOX_MESSAGE,
                        source=msg.get("from", "unknown"),
                        payload={
                            "message_type": msg_type,
                            "from": msg.get("from", "unknown"),
                            "to": msg.get("to", profile_dir.name),
                            "file": file_key,
                            "summary": self._summarize(msg),
                        },
                        correlation_id=msg.get("correlation_id"),
                        job_id=msg.get("job_id"),
                    )
                    count += 1
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to read mailbox message %s: %s", msg_file, e)

        if count:
            self._save_watermark()

        return count

    def _is_protocol_message(self, filename: str) -> bool:
        """Check if filename matches the protocol naming convention:
        {timestamp}_{TYPE}_{from}.json
        """
        parts = filename.rsplit(".", 1)[0].split("_", 2)
        return len(parts) >= 2

    def _summarize(self, msg: dict) -> str:
        """Create a short human-readable summary of the message payload."""
        payload = msg.get("payload", {})
        msg_type = msg.get("type", "")

        if msg_type == "SCORE_BATCH_SUMMARY":
            jobs = payload.get("scored_jobs", [])
            return f"{len(jobs)} jobs scored"
        if msg_type == "SCOUT_DISCOVERY":
            jobs = payload.get("jobs", [])
            return f"{len(jobs)} jobs discovered"
        if msg_type in ("TAILOR_REQUEST", "TAILOR_COMPLETE"):
            return payload.get("job_title", msg_type)
        if msg_type == "ERROR":
            return payload.get("message", "Error")[:200]

        return msg_type
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_mailbox_watcher.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/producers/mailbox_watcher.py tests/events/test_mailbox_watcher.py
git commit -m "feat(events): add MailboxWatcher — polls inter-agent mailbox for protocol messages"
```

---

## Task 8: TelegramNotifier Subscriber

**Files:**
- Create: `events/subscribers/telegram_notifier.py`
- Test: `tests/events/test_telegram_notifier.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_telegram_notifier.py`:

```python
"""Tests for events.subscribers.telegram_notifier — Telegram forum topic routing."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.telegram_notifier import TelegramNotifier, TOPIC_ROUTING


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def topics_config(tmp_path):
    config = {
        "group_chat_id": "-1001234567890",
        "topics": {
            "alerts": {"thread_id": 100, "name": "Alerts & Actions"},
            "scout": {"thread_id": 101, "name": "Scout / Discoveries"},
            "matcher": {"thread_id": 102, "name": "Matcher / Scores"},
            "tailor_applier": {"thread_id": 103, "name": "Tailor & Applier"},
            "tracker": {"thread_id": 104, "name": "Tracker / Pipeline"},
            "digests": {"thread_id": 105, "name": "Digests & Summaries"},
            "system": {"thread_id": 106, "name": "System Health"},
            "agent_comms": {"thread_id": 107, "name": "Agent Comms"},
        },
    }
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    config = {
        "scout": {"mode": "all"},
        "matcher": {"mode": "all"},
        "system": {"mode": "digest_only"},
        "agent_comms": {"mode": "significant_only"},
    }
    path = tmp_path / "telegram" / "verbosity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


class TestTopicRouting:
    def test_all_event_types_have_routing(self):
        for et in EventType:
            assert et.type_string in TOPIC_ROUTING, \
                f"EventType {et.type_string} missing from TOPIC_ROUTING"

    def test_scout_events_route_to_scout(self):
        assert TOPIC_ROUTING["job_discovered"] == "scout"
        assert TOPIC_ROUTING["job_vip_discovered"] == "scout"

    def test_critical_events_route_to_alerts(self):
        assert TOPIC_ROUTING["application_blocked"] == "alerts"
        assert TOPIC_ROUTING["interview_signal"] == "alerts"
        assert TOPIC_ROUTING["offer_signal"] == "alerts"


class TestTelegramNotifier:
    def test_formats_message(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "VP Finance", "company": "Acme", "source": "Indeed"},
        )
        msg = notifier.format_message(event)
        assert "job_discovered" in msg.lower() or "JOB_DISCOVERED" in msg
        assert "scout" in msg.lower()

    def test_resolves_topic_for_event(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(EventType.JOB_DISCOVERED, "scout", {})
        target = notifier.resolve_target(event)
        assert target == ("telegram", "-1001234567890", "101")

    def test_cross_posts_critical_to_alerts(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_FAILED, "applier", {"error": "timeout"},
            priority=Priority.CRITICAL,
        )
        targets = notifier.resolve_all_targets(event)
        topic_ids = [t[2] for t in targets]
        # Should be in both tailor_applier (natural) AND alerts (cross-post)
        # application_failed routes to alerts directly, so just alerts
        assert "100" in topic_ids  # alerts

    def test_loads_topics_config(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        assert notifier.group_chat_id == "-1001234567890"
        assert notifier.topics["scout"]["thread_id"] == 101
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_telegram_notifier.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement TelegramNotifier**

Create `events/subscribers/telegram_notifier.py`:

```python
"""TelegramNotifier subscriber — routes events to Telegram forum topics.

Reads topic registry from ~/.hermes/telegram/topics.json and verbosity
config from ~/.hermes/telegram/verbosity.json.  Delivers messages via
the gateway's Telegram adapter send() method.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Maps event_type string → topic key
TOPIC_ROUTING: Dict[str, str] = {
    # Alerts & Actions
    "application_blocked": "alerts",
    "application_failed": "alerts",
    "interview_signal": "alerts",
    "offer_signal": "alerts",
    "cron_failed_consecutive": "alerts",
    "gateway_health": "alerts",
    # Scout
    "job_discovered": "scout",
    "job_vip_discovered": "scout",
    # Matcher
    "job_scored": "matcher",
    "job_high_score": "matcher",
    # Tailor & Applier
    "tailor_completed": "tailor_applier",
    "application_ready": "tailor_applier",
    "application_submitted": "tailor_applier",
    # Tracker
    "stage_transition": "tracker",
    "followup_due": "tracker",
    # Digests
    "digest_generated": "digests",
    # System Health
    "cron_started": "system",
    "cron_completed": "system",
    "cron_failed": "system",
    "agent_error": "system",
    "memory_consolidated": "system",
    "skill_evolved": "system",
    # Agent Comms
    "mailbox_message": "agent_comms",
}

# Events that cross-post to alerts when high/critical
CROSS_POST_TO_ALERTS = {
    "job_high_score", "application_ready", "followup_due",
}


class TelegramNotifier(BaseSubscriber):
    subscriber_id = "telegram-notifier"
    poll_interval_seconds = 5

    def __init__(
        self,
        bus: EventBus,
        topics_path: Optional[Path] = None,
        verbosity_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if topics_path is None:
            from hermes_constants import get_hermes_home
            topics_path = get_hermes_home() / "telegram" / "topics.json"
        if verbosity_path is None:
            from hermes_constants import get_hermes_home
            verbosity_path = get_hermes_home() / "telegram" / "verbosity.json"

        self._topics_path = Path(topics_path)
        self._verbosity_path = Path(verbosity_path)
        self._send_fn = send_fn  # injected for testing; uses gateway adapter in prod

        self.group_chat_id: str = ""
        self.topics: Dict[str, Dict[str, Any]] = {}
        self._verbosity: Dict[str, Dict[str, str]] = {}
        self._batch_buffer: Dict[str, List[str]] = {}  # topic_key → messages

        self._load_config()

    def _load_config(self) -> None:
        """Load topic registry and verbosity config from disk."""
        if self._topics_path.exists():
            data = json.loads(self._topics_path.read_text(encoding="utf-8"))
            self.group_chat_id = data.get("group_chat_id", "")
            self.topics = data.get("topics", {})
        if self._verbosity_path.exists():
            self._verbosity = json.loads(self._verbosity_path.read_text(encoding="utf-8"))

    def handle(self, event: Event) -> None:
        if not self.group_chat_id or not self.topics:
            self._load_config()
            if not self.group_chat_id:
                logger.debug("TelegramNotifier: no topics.json configured, skipping")
                return

        targets = self.resolve_all_targets(event)
        message = self.format_message(event)

        for platform, chat_id, thread_id in targets:
            topic_key = self._thread_id_to_key(thread_id)
            verbosity = self._verbosity.get(topic_key, {}).get("mode", "all")

            if verbosity == "off":
                continue
            if verbosity == "significant_only" and event.priority.level < Priority.HIGH.level:
                continue
            # digest_only mode: would batch and send every 30 min
            # For now, skip low-priority in digest_only topics
            if verbosity == "digest_only" and event.priority.level < Priority.HIGH.level:
                continue

            self._deliver(chat_id, thread_id, message)

    def resolve_target(self, event: Event) -> Tuple[str, str, str]:
        """Resolve the primary Telegram target for an event."""
        topic_key = TOPIC_ROUTING.get(event.event_type.type_string, "system")
        topic = self.topics.get(topic_key, {})
        thread_id = str(topic.get("thread_id", ""))
        return ("telegram", self.group_chat_id, thread_id)

    def resolve_all_targets(self, event: Event) -> List[Tuple[str, str, str]]:
        """Resolve all targets including cross-posts."""
        targets = [self.resolve_target(event)]

        # Cross-post action-required high/critical events to alerts
        if (event.event_type.type_string in CROSS_POST_TO_ALERTS
                and event.priority.level >= Priority.HIGH.level):
            alerts_topic = self.topics.get("alerts", {})
            alerts_thread = str(alerts_topic.get("thread_id", ""))
            primary_thread = targets[0][2]
            if alerts_thread and alerts_thread != primary_thread:
                targets.append(("telegram", self.group_chat_id, alerts_thread))

        return targets

    def format_message(self, event: Event) -> str:
        """Format an event into a human-readable Telegram message."""
        ts = event.timestamp[:19].replace("T", " ")
        priority_label = event.priority.label.upper()
        header = f"[{priority_label}] {event.event_type.type_string} from {event.source} @ {ts} UTC"

        body = self._format_payload(event)
        return f"{header}\n{body}" if body else header

    def _format_payload(self, event: Event) -> str:
        """Format event payload into readable text."""
        p = event.payload
        et = event.event_type

        if et == EventType.CRON_COMPLETED:
            summary = p.get("output_summary", "")
            duration = p.get("duration", "?")
            return f"Duration: {duration}s\n{summary}" if summary else f"Duration: {duration}s"

        if et == EventType.CRON_FAILED:
            return f"Error: {p.get('error', 'Unknown')}\nConsecutive failures: {p.get('consecutive_errors', 0)}"

        if et == EventType.JOB_DISCOVERED:
            return f"Title: {p.get('title', '?')}\nCompany: {p.get('company', '?')}\nSource: {p.get('source', '?')}"

        if et in (EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE):
            return f"Score: {p.get('score', '?')}\nTitle: {p.get('title', '?')}\nCompany: {p.get('company', '?')}"

        if et == EventType.APPLICATION_FAILED:
            return f"Error: {p.get('error', 'Unknown')}\nCompany: {p.get('company', '?')}"

        if et == EventType.GATEWAY_HEALTH:
            return f"Platform: {p.get('platform', '?')} → {p.get('status', '?')}\n{p.get('detail', '')}"

        if et == EventType.MAILBOX_MESSAGE:
            return f"{p.get('from', '?')} → {p.get('to', '?')}: {p.get('message_type', '?')}\n{p.get('summary', '')}"

        # Generic fallback
        lines = [f"{k}: {v}" for k, v in p.items() if v]
        return "\n".join(lines[:10])

    def _deliver(self, chat_id: str, thread_id: str, message: str) -> None:
        """Send a message to a Telegram chat/thread."""
        if self._send_fn:
            self._send_fn(chat_id, thread_id, message)
            return

        # Production: use gateway delivery
        try:
            from cron.scheduler import _deliver_result
            target_str = f"telegram:{chat_id}:{thread_id}" if thread_id else f"telegram:{chat_id}"
            _deliver_result(
                {"deliver": target_str, "id": "event-bus", "name": "event-bus"},
                message,
            )
        except Exception as e:
            logger.error("TelegramNotifier delivery failed: %s", e)

    def _thread_id_to_key(self, thread_id: str) -> str:
        """Reverse lookup: thread_id → topic key."""
        for key, topic in self.topics.items():
            if str(topic.get("thread_id", "")) == thread_id:
                return key
        return "system"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_telegram_notifier.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/telegram_notifier.py tests/events/test_telegram_notifier.py
git commit -m "feat(events): add TelegramNotifier — routes events to forum topics"
```

---

## Task 9: WhatsAppEscalator Subscriber

**Files:**
- Create: `events/subscribers/whatsapp_escalator.py`
- Test: `tests/events/test_whatsapp_escalator.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_whatsapp_escalator.py`:

```python
"""Tests for events.subscribers.whatsapp_escalator — WhatsApp escalation with quiet hours."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.whatsapp_escalator import WhatsAppEscalator


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def quiet_config(tmp_path):
    config = {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
    }
    path = tmp_path / "notifications" / "quiet_hours.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / "notifications" / "quiet_queue.json"


class TestEscalationCriteria:
    def test_interview_signal_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Google"})
        assert escalator.should_escalate(event) is True

    def test_cron_completed_does_not_escalate(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.CRON_COMPLETED, "scout", {})
        assert escalator.should_escalate(event) is False

    def test_job_high_score_above_9_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.JOB_HIGH_SCORE, "matcher", {"score": 9.1})
        assert escalator.should_escalate(event) is True

    def test_job_high_score_below_9_does_not_escalate(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.JOB_HIGH_SCORE, "matcher", {"score": 8.8})
        assert escalator.should_escalate(event) is False

    def test_application_blocked_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})
        assert escalator.should_escalate(event) is True

    def test_cron_failed_consecutive_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.CRON_FAILED_CONSECUTIVE, "system", {})
        assert escalator.should_escalate(event) is True


class TestQuietHours:
    def test_breakthrough_during_quiet_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Acme"})

        # Simulate 2am ET (quiet hours)
        with patch.object(escalator, '_is_quiet_hours', return_value=True):
            assert escalator.should_deliver_now(event) is True  # breakthrough

    def test_non_breakthrough_queued_during_quiet_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=True):
            assert escalator.should_deliver_now(event) is False

    def test_all_events_deliver_during_active_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            assert escalator.should_deliver_now(event) is True


class TestMessageFormat:
    def test_plain_text_no_markdown(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "What is your visa status?"},
        )
        msg = escalator.format_message(event)
        assert "**" not in msg  # no markdown bold
        assert "Acme" in msg
        assert "Details in Telegram" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_whatsapp_escalator.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement WhatsAppEscalator**

Create `events/subscribers/whatsapp_escalator.py`:

```python
"""WhatsAppEscalator — sends escalated notifications to WhatsApp.

Filters events by escalation criteria, respects quiet hours (11pm-7am ET),
and queues non-breakthrough events for morning flush.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Events that escalate to WhatsApp
ESCALATION_EVENTS = {
    # Immediate (breakthrough during quiet hours)
    EventType.INTERVIEW_SIGNAL,
    EventType.OFFER_SIGNAL,
    # Urgent
    EventType.APPLICATION_BLOCKED,
    EventType.APPLICATION_FAILED,
    EventType.CRON_FAILED_CONSECUTIVE,
    EventType.GATEWAY_HEALTH,
    # Important
    EventType.JOB_HIGH_SCORE,  # only if score >= 9.0
    EventType.APPLICATION_READY,
    EventType.FOLLOWUP_DUE,
}

BREAKTHROUGH_EVENTS = {EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL}

HIGH_SCORE_WA_THRESHOLD = 9.0


class WhatsAppEscalator(BaseSubscriber):
    subscriber_id = "whatsapp-escalator"
    poll_interval_seconds = 10

    def __init__(
        self,
        bus: EventBus,
        quiet_config_path: Optional[Path] = None,
        queue_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if quiet_config_path is None:
            from hermes_constants import get_hermes_home
            quiet_config_path = get_hermes_home() / "notifications" / "quiet_hours.json"
        if queue_path is None:
            from hermes_constants import get_hermes_home
            queue_path = get_hermes_home() / "notifications" / "quiet_queue.json"

        self._quiet_config_path = Path(quiet_config_path)
        self._queue_path = Path(queue_path)
        self._send_fn = send_fn
        self._quiet_config = self._load_quiet_config()

    def _load_quiet_config(self) -> Dict[str, Any]:
        if self._quiet_config_path.exists():
            return json.loads(self._quiet_config_path.read_text(encoding="utf-8"))
        return {
            "enabled": True,
            "start": "23:00",
            "end": "07:00",
            "timezone": "America/New_York",
            "breakthrough_events": ["interview_signal", "offer_signal"],
        }

    def should_escalate(self, event: Event) -> bool:
        """Check if this event meets WhatsApp escalation criteria."""
        if event.event_type not in ESCALATION_EVENTS:
            return False

        # JOB_HIGH_SCORE only escalates if score >= 9.0
        if event.event_type == EventType.JOB_HIGH_SCORE:
            score = event.payload.get("score", 0)
            return score >= HIGH_SCORE_WA_THRESHOLD

        # GATEWAY_HEALTH only escalates on "down"
        if event.event_type == EventType.GATEWAY_HEALTH:
            return event.payload.get("status") == "down"

        return True

    def should_deliver_now(self, event: Event) -> bool:
        """Check if event should be delivered now vs queued for morning."""
        if not self._is_quiet_hours():
            return True
        return event.event_type in BREAKTHROUGH_EVENTS

    def _is_quiet_hours(self) -> bool:
        """Check if current time is within quiet hours."""
        if not self._quiet_config.get("enabled", True):
            return False
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(self._quiet_config.get("timezone", "America/New_York"))
            now = datetime.now(tz)
            start_h, start_m = map(int, self._quiet_config["start"].split(":"))
            end_h, end_m = map(int, self._quiet_config["end"].split(":"))

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_h * 60 + start_m
            end_minutes = end_h * 60 + end_m

            if start_minutes > end_minutes:  # crosses midnight (23:00-07:00)
                return current_minutes >= start_minutes or current_minutes < end_minutes
            return start_minutes <= current_minutes < end_minutes
        except Exception:
            return False

    def handle(self, event: Event) -> None:
        if not self.should_escalate(event):
            return

        message = self.format_message(event)

        if self.should_deliver_now(event):
            self._deliver(message)
        else:
            self._queue_message(message)

    def format_message(self, event: Event) -> str:
        """Format event as plain-text WhatsApp message."""
        p = event.payload
        et = event.event_type

        if et == EventType.INTERVIEW_SIGNAL:
            text = f"Interview signal from {p.get('company', '?')}. {p.get('detail', '')}"
        elif et == EventType.OFFER_SIGNAL:
            text = f"Offer received from {p.get('company', '?')}! {p.get('detail', '')}"
        elif et == EventType.APPLICATION_BLOCKED:
            text = f"Application blocked at {p.get('company', '?')}: {p.get('question', 'needs your input')}"
        elif et == EventType.APPLICATION_FAILED:
            text = f"Application failed for {p.get('company', '?')}: {p.get('error', 'unknown error')}"
        elif et == EventType.APPLICATION_READY:
            text = f"Dry-run complete for {p.get('company', '?')} {p.get('title', '')}. Approve submission? Reply YES or NO."
        elif et == EventType.JOB_HIGH_SCORE:
            text = f"High-score job: {p.get('title', '?')} at {p.get('company', '?')} scored {p.get('score', '?')}"
        elif et == EventType.CRON_FAILED_CONSECUTIVE:
            text = f"Cron job '{p.get('job_name', '?')}' has failed {p.get('consecutive_errors', '?')} times in a row: {p.get('error', '')}"
        elif et == EventType.GATEWAY_HEALTH:
            text = f"Gateway {p.get('platform', '?')} is DOWN. {p.get('detail', '')}"
        elif et == EventType.FOLLOWUP_DUE:
            text = f"Follow-up due for {p.get('company', '?')} — {p.get('days', 14)}+ days no response"
        else:
            text = f"{et.type_string}: {json.dumps(p)[:200]}"

        return f"{text.strip()}\n\nDetails in Telegram"

    def _deliver(self, message: str) -> None:
        """Send message via WhatsApp."""
        if self._send_fn:
            self._send_fn(message)
            return
        try:
            from cron.scheduler import _deliver_result
            _deliver_result(
                {"deliver": "whatsapp", "id": "event-bus", "name": "event-bus"},
                message,
            )
        except Exception as e:
            logger.error("WhatsApp delivery failed: %s", e)

    def _queue_message(self, message: str) -> None:
        """Queue message for morning flush."""
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = []
        if self._queue_path.exists():
            try:
                queue = json.loads(self._queue_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                queue = []
        queue.append({
            "message": message,
            "queued_at": datetime.now().isoformat(),
        })
        self._queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")

    def flush_queue(self) -> int:
        """Flush queued messages as overnight summary.  Returns count flushed."""
        if not self._queue_path.exists():
            return 0
        try:
            queue = json.loads(self._queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0
        if not queue:
            return 0

        messages = [item["message"].split("\n\nDetails in Telegram")[0] for item in queue]
        summary = "Overnight Summary — {} events while you were away:\n\n".format(len(messages))
        summary += "\n\n".join(f"- {m}" for m in messages)
        summary += "\n\nDetails in Telegram"

        self._deliver(summary)
        self._queue_path.write_text("[]", encoding="utf-8")
        return len(queue)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_whatsapp_escalator.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/whatsapp_escalator.py tests/events/test_whatsapp_escalator.py
git commit -m "feat(events): add WhatsAppEscalator — quiet-hours-aware escalation"
```

---

## Task 10: DigestComposer Subscriber

**Files:**
- Create: `events/subscribers/digest_composer.py`
- Test: `tests/events/test_digest_composer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_digest_composer.py`:

```python
"""Tests for events.subscribers.digest_composer — 3x/day structured digests."""

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.digest_composer import DigestComposer


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


class TestDigestComposer:
    def test_compose_from_events(self, bus):
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "VP Finance", "source": "Indeed"})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "FP&A Dir", "source": "LinkedIn"})
        bus.emit(EventType.JOB_SCORED, "matcher", {"title": "VP Finance", "score": 8.5})
        bus.emit(EventType.CRON_COMPLETED, "jobflow-scout", {"duration": 120})
        bus.emit(EventType.CRON_COMPLETED, "jobflow-matcher", {"duration": 45})

        composer = DigestComposer(bus)
        digest = composer.compose()

        assert "HERMES DIGEST" in digest
        assert "scout" in digest.lower() or "Scout" in digest
        assert "2" in digest  # 2 jobs discovered

    def test_compose_empty_when_no_events(self, bus):
        composer = DigestComposer(bus)
        digest = composer.compose()
        assert "No activity" in digest or "HERMES DIGEST" in digest

    def test_compose_includes_action_items(self, bus):
        bus.emit(EventType.APPLICATION_READY, "applier", {"company": "Acme", "title": "VP Tax"})
        bus.emit(EventType.FOLLOWUP_DUE, "tracker", {"company": "Deloitte", "days": 14})

        composer = DigestComposer(bus)
        digest = composer.compose()

        assert "ACTION" in digest.upper()
        assert "Acme" in digest
        assert "Deloitte" in digest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_digest_composer.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement DigestComposer**

Create `events/subscribers/digest_composer.py`:

```python
"""DigestComposer — produces 3x/day structured notification digests.

Timer-based subscriber that fires at 8am, 1pm, and 6pm.  Queries the
event bus for events since the last digest and formats a structured summary.
Posts to the Digests & Summaries Telegram topic and WhatsApp (morning only).
"""

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

DIGEST_SCHEDULE_HOURS = [8, 13, 18]  # ET


class DigestComposer(BaseSubscriber):
    subscriber_id = "digest-composer"
    poll_interval_seconds = 60  # check every minute if digest is due

    def __init__(
        self,
        bus: EventBus,
        send_telegram_fn: Optional[Callable] = None,
        send_whatsapp_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        self._send_telegram_fn = send_telegram_fn
        self._send_whatsapp_fn = send_whatsapp_fn
        self._last_digest_at: Optional[str] = None

    def handle(self, event: Event) -> None:
        # DigestComposer doesn't process individual events via handle().
        # It uses compose() triggered by the timer.  This is a no-op so the
        # base subscriber can still poll and ack to advance the cursor.
        pass

    def compose(self, since: Optional[str] = None) -> str:
        """Compose a digest from events since the given timestamp (or last digest)."""
        query_since = since or self._last_digest_at
        events = self.bus.query(since=query_since) if query_since else self.bus.query()
        self._last_digest_at = datetime.now(timezone.utc).isoformat()

        if not events:
            return self._format_empty_digest()

        return self._format_digest(events)

    def _format_digest(self, events: List[Event]) -> str:
        """Format a list of events into a structured digest."""
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()

        # Count events by type
        type_counts: Counter = Counter()
        source_counts: Counter = Counter()
        action_items: List[str] = []
        highlights: List[str] = []
        errors: List[str] = []

        for e in events:
            type_counts[e.event_type] += 1
            source_counts[e.source] += 1

            if e.event_type == EventType.APPLICATION_READY:
                action_items.append(
                    f"Approve dry-run for {e.payload.get('company', '?')} "
                    f"{e.payload.get('title', '')}".strip()
                )
            elif e.event_type == EventType.FOLLOWUP_DUE:
                action_items.append(
                    f"Follow up with {e.payload.get('company', '?')} "
                    f"({e.payload.get('days', 14)}+ days)"
                )
            elif e.event_type == EventType.APPLICATION_BLOCKED:
                action_items.append(
                    f"Unblock application at {e.payload.get('company', '?')}: "
                    f"{e.payload.get('question', 'needs input')}"
                )

            if e.event_type == EventType.JOB_HIGH_SCORE:
                highlights.append(
                    f"{e.payload.get('title', '?')} at {e.payload.get('company', '?')} "
                    f"scored {e.payload.get('score', '?')}"
                )
            elif e.event_type in (EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL):
                highlights.append(
                    f"{e.event_type.type_string.upper()}: {e.payload.get('company', '?')}"
                )

            if e.event_type in (EventType.CRON_FAILED, EventType.CRON_FAILED_CONSECUTIVE, EventType.AGENT_ERROR):
                errors.append(f"{e.source}: {e.payload.get('error', 'unknown')[:100]}")

        # Build digest
        lines = [f"HERMES DIGEST — {period} / {now_str}", ""]

        # Event summary by source
        lines.append("SINCE LAST DIGEST")
        discovered = type_counts.get(EventType.JOB_DISCOVERED, 0)
        scored = type_counts.get(EventType.JOB_SCORED, 0) + type_counts.get(EventType.JOB_HIGH_SCORE, 0)
        tailored = type_counts.get(EventType.TAILOR_COMPLETED, 0)
        submitted = type_counts.get(EventType.APPLICATION_SUBMITTED, 0)
        transitions = type_counts.get(EventType.STAGE_TRANSITION, 0)

        if discovered:
            lines.append(f"  Scout: {discovered} new jobs found")
        if scored:
            high = type_counts.get(EventType.JOB_HIGH_SCORE, 0)
            lines.append(f"  Matcher: {scored} scored — {high} HIGH (>=8.75)")
        if tailored:
            lines.append(f"  Tailor: {tailored} resumes generated")
        if submitted:
            lines.append(f"  Applier: {submitted} submitted")
        if transitions:
            lines.append(f"  Tracker: {transitions} stage transitions")
        if not any([discovered, scored, tailored, submitted, transitions]):
            lines.append("  No activity since last digest")

        # Highlights
        if highlights:
            lines.append("")
            lines.append("HIGHLIGHTS")
            for h in highlights:
                lines.append(f"  {h}")

        # Action items
        if action_items:
            lines.append("")
            lines.append("ACTION ITEMS")
            for item in action_items:
                lines.append(f"  -> {item}")

        # Errors
        if errors:
            lines.append("")
            lines.append("SYSTEM HEALTH")
            for err in errors:
                lines.append(f"  ! {err}")
        else:
            lines.append("")
            lines.append("SYSTEM HEALTH")
            cron_ok = type_counts.get(EventType.CRON_COMPLETED, 0)
            lines.append(f"  {cron_ok} cron jobs completed OK")

        return "\n".join(lines)

    def _format_empty_digest(self) -> str:
        now_str = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
        period = self._get_period_label()
        return f"HERMES DIGEST — {period} / {now_str}\n\nNo activity since last digest."

    def _get_period_label(self) -> str:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo("America/New_York")
            hour = datetime.now(tz).hour
        except Exception:
            hour = datetime.now().hour

        if hour < 12:
            return "Morning"
        if hour < 17:
            return "Midday"
        return "Evening"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_digest_composer.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/digest_composer.py tests/events/test_digest_composer.py
git commit -m "feat(events): add DigestComposer — 3x/day structured notification digests"
```

---

## Task 11: MemoryWriter Subscriber

**Files:**
- Create: `events/subscribers/memory_writer.py`
- Test: `tests/events/test_memory_writer.py`

- [ ] **Step 1: Write failing tests**

Create `tests/events/test_memory_writer.py`:

```python
"""Tests for events.subscribers.memory_writer — routes events to memory layers."""

from unittest.mock import MagicMock, patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.memory_writer import MemoryWriter, MEMORY_ROUTING


class TestMemoryRouting:
    def test_high_score_routes_to_gbrain(self):
        assert "gbrain" in MEMORY_ROUTING[EventType.JOB_HIGH_SCORE]["targets"]

    def test_interview_routes_to_both(self):
        targets = MEMORY_ROUTING[EventType.INTERVIEW_SIGNAL]["targets"]
        assert "gbrain" in targets
        assert "mempalace" in targets

    def test_cron_failed_consecutive_routes_to_memory_md(self):
        assert "memory_md" in MEMORY_ROUTING[EventType.CRON_FAILED_CONSECUTIVE]["targets"]

    def test_cron_completed_not_in_routing(self):
        assert EventType.CRON_COMPLETED not in MEMORY_ROUTING


class TestMemoryWriter:
    def test_skips_non_routed_events(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(EventType.CRON_COMPLETED, "scout", {})
        # Should not raise
        writer.handle(event)

    def test_builds_gbrain_content(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
        )
        content = writer._build_content(event, "gbrain")
        assert "Acme" in content
        assert "VP Finance" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_memory_writer.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement MemoryWriter**

Create `events/subscribers/memory_writer.py`:

```python
"""MemoryWriter subscriber — routes high-signal events to memory layers.

Follows CLAUDE.md memory routing rules:
  - GBrain: world knowledge (companies, jobs, applications)
  - MemPalace: verbatim evidence (interview signals, offers)
  - Agent MEMORY.md: operational notes (failures, outages)

Rate-limited: max 10 GBrain writes/hour, 5 MemPalace writes/hour.
"""

import logging
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

# Maps EventType → {targets: [layer], template: str}
MEMORY_ROUTING: Dict[EventType, Dict[str, Any]] = {
    EventType.JOB_HIGH_SCORE: {
        "targets": ["gbrain"],
        "action": "put_page_and_timeline",
    },
    EventType.APPLICATION_SUBMITTED: {
        "targets": ["gbrain"],
        "action": "add_timeline_entry",
    },
    EventType.APPLICATION_FAILED: {
        "targets": ["gbrain", "memory_md"],
        "action": "add_timeline_and_note",
    },
    EventType.INTERVIEW_SIGNAL: {
        "targets": ["gbrain", "mempalace"],
        "action": "timeline_and_evidence",
    },
    EventType.OFFER_SIGNAL: {
        "targets": ["gbrain", "mempalace"],
        "action": "timeline_and_evidence",
    },
    EventType.STAGE_TRANSITION: {
        "targets": ["gbrain"],
        "action": "add_timeline_entry",
    },
    EventType.CRON_FAILED_CONSECUTIVE: {
        "targets": ["memory_md"],
        "action": "operational_note",
    },
    EventType.GATEWAY_HEALTH: {
        "targets": ["memory_md"],
        "action": "operational_note",
    },
    EventType.FOLLOWUP_DUE: {
        "targets": ["mempalace"],
        "action": "add_drawer",
    },
}

RATE_LIMITS = {
    "gbrain": {"max_per_hour": 10, "window": 3600},
    "mempalace": {"max_per_hour": 5, "window": 3600},
    "memory_md": {"max_per_hour": 20, "window": 3600},
}


class MemoryWriter(BaseSubscriber):
    subscriber_id = "memory-writer"
    poll_interval_seconds = 10

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self._rate_counters: Dict[str, List[float]] = defaultdict(list)

    def handle(self, event: Event) -> None:
        routing = MEMORY_ROUTING.get(event.event_type)
        if not routing:
            return

        for target in routing["targets"]:
            if not self._check_rate_limit(target):
                logger.warning("MemoryWriter: rate limit hit for %s, queuing", target)
                continue
            try:
                content = self._build_content(event, target)
                self._write_to_target(target, event, content)
            except Exception:
                logger.exception("MemoryWriter: failed to write to %s for %s",
                                 target, event.event_type.type_string)

    def _check_rate_limit(self, target: str) -> bool:
        """Check if we're within rate limits for this target."""
        limits = RATE_LIMITS.get(target, {"max_per_hour": 20, "window": 3600})
        now = time.monotonic()
        window = limits["window"]

        # Clean old entries
        self._rate_counters[target] = [
            t for t in self._rate_counters[target] if now - t < window
        ]

        if len(self._rate_counters[target]) >= limits["max_per_hour"]:
            return False

        self._rate_counters[target].append(now)
        return True

    def _build_content(self, event: Event, target: str) -> str:
        """Build content string for the target memory layer."""
        p = event.payload
        et = event.event_type

        if target == "gbrain":
            if et == EventType.JOB_HIGH_SCORE:
                return (f"High-score job discovered: {p.get('title', '?')} at "
                        f"{p.get('company', '?')} (score: {p.get('score', '?')})")
            if et == EventType.APPLICATION_SUBMITTED:
                return (f"Application submitted for {p.get('title', '?')} at "
                        f"{p.get('company', '?')} via {p.get('platform', '?')} "
                        f"on {event.timestamp[:10]}")
            if et == EventType.APPLICATION_FAILED:
                return (f"Application failed for {p.get('title', '?')} at "
                        f"{p.get('company', '?')}: {p.get('error', 'unknown')}")
            if et in (EventType.INTERVIEW_SIGNAL, EventType.OFFER_SIGNAL):
                return (f"{et.type_string}: {p.get('company', '?')} — "
                        f"{p.get('detail', 'no detail')}")
            if et == EventType.STAGE_TRANSITION:
                return (f"Pipeline: {p.get('company', '?')} moved to "
                        f"{p.get('new_stage', '?')} from {p.get('old_stage', '?')}")

        if target == "mempalace":
            # Verbatim evidence
            return (f"[{event.timestamp}] {et.type_string} from {event.source}: "
                    f"{p.get('company', '?')} — {p.get('detail', str(p)[:300])}")

        if target == "memory_md":
            if et == EventType.CRON_FAILED_CONSECUTIVE:
                return (f"{p.get('job_name', '?')} failing since {event.timestamp[:10]} "
                        f"({p.get('consecutive_errors', '?')} consecutive) — "
                        f"{p.get('error', 'investigate')}")
            if et == EventType.GATEWAY_HEALTH:
                return (f"{p.get('platform', '?')} gateway went {p.get('status', '?')} "
                        f"at {event.timestamp[:19]}")
            if et == EventType.APPLICATION_FAILED:
                return (f"Application to {p.get('company', '?')} failed: "
                        f"{p.get('error', 'unknown')} — investigate {p.get('platform', '?')} compatibility")

        return f"{et.type_string}: {str(p)[:200]}"

    def _write_to_target(self, target: str, event: Event, content: str) -> None:
        """Write content to the specified memory layer."""
        if target == "gbrain":
            self._write_gbrain(event, content)
        elif target == "mempalace":
            self._write_mempalace(event, content)
        elif target == "memory_md":
            self._write_memory_md(event, content)

    def _write_gbrain(self, event: Event, content: str) -> None:
        """Write to GBrain via MCP tools (best-effort)."""
        try:
            # GBrain is available as MCP - attempt via subprocess
            import subprocess
            company = event.payload.get("company", "")
            if not company:
                logger.debug("MemoryWriter: no company in event, skipping GBrain write")
                return
            # Use gbrain CLI if available
            subprocess.run(
                ["gbrain", "timeline", "add", company, content],
                capture_output=True, timeout=10,
            )
            logger.info("MemoryWriter: wrote GBrain timeline entry for %s", company)
        except FileNotFoundError:
            logger.debug("MemoryWriter: gbrain CLI not found, skipping")
        except Exception as e:
            logger.warning("MemoryWriter: GBrain write failed: %s", e)

    def _write_mempalace(self, event: Event, content: str) -> None:
        """Write to MemPalace via MCP tools (best-effort)."""
        logger.info("MemoryWriter: would write to MemPalace: %s", content[:100])
        # MemPalace writes will be integrated via MCP in the gateway context

    def _write_memory_md(self, event: Event, content: str) -> None:
        """Append operational note to agent MEMORY.md."""
        try:
            from hermes_constants import get_hermes_home
            memory_path = get_hermes_home() / "memories" / "MEMORY.md"
            memory_path.parent.mkdir(parents=True, exist_ok=True)

            existing = ""
            if memory_path.exists():
                existing = memory_path.read_text(encoding="utf-8")

            # Append with date header
            date_header = f"\n## Event {event.timestamp[:10]}\n"
            if date_header.strip() not in existing:
                existing += date_header
            existing += f"- {content}\n"

            memory_path.write_text(existing, encoding="utf-8")
            logger.info("MemoryWriter: appended to MEMORY.md: %s", content[:80])
        except Exception as e:
            logger.warning("MemoryWriter: MEMORY.md write failed: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_memory_writer.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/memory_writer.py tests/events/test_memory_writer.py
git commit -m "feat(events): add MemoryWriter — routes high-signal events to memory layers"
```

---

## Task 12: TelegramMirror Subscriber

**Files:**
- Create: `events/subscribers/telegram_mirror.py`
- Test: `tests/events/test_telegram_mirror.py`

- [ ] **Step 1: Write failing test**

Create `tests/events/test_telegram_mirror.py`:

```python
"""Tests for events.subscribers.telegram_mirror — mirrors mailbox events to Agent Comms topic."""

import pytest

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.telegram_mirror import TelegramMirror


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "test.db")


class TestTelegramMirror:
    def test_only_processes_mailbox_events(self, bus):
        mirror = TelegramMirror(bus)
        assert mirror.event_types == [EventType.MAILBOX_MESSAGE]

    def test_formats_mirror_message(self, bus):
        mirror = TelegramMirror(bus)
        event = Event.create(
            EventType.MAILBOX_MESSAGE, "matcher",
            {"message_type": "SCORE_RESULT", "from": "matcher", "to": "main",
             "summary": "3 jobs scored"},
        )
        msg = mirror.format_mirror_message(event)
        assert "matcher" in msg
        assert "main" in msg
        assert "SCORE_RESULT" in msg
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_telegram_mirror.py -v`

Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement TelegramMirror**

Create `events/subscribers/telegram_mirror.py`:

```python
"""TelegramMirror — shadow-copies inter-agent mailbox messages to Telegram.

Subscribes only to mailbox_message events (emitted by MailboxWatcher)
and posts formatted summaries to the Agent Comms topic.
"""

import json
import logging
from pathlib import Path
from typing import Callable, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)


class TelegramMirror(BaseSubscriber):
    subscriber_id = "telegram-mirror"
    poll_interval_seconds = 60
    event_types = [EventType.MAILBOX_MESSAGE]

    def __init__(
        self,
        bus: EventBus,
        topics_path: Optional[Path] = None,
        send_fn: Optional[Callable] = None,
    ):
        super().__init__(bus)
        if topics_path is None:
            from hermes_constants import get_hermes_home
            topics_path = get_hermes_home() / "telegram" / "topics.json"
        self._topics_path = Path(topics_path)
        self._send_fn = send_fn

    def handle(self, event: Event) -> None:
        message = self.format_mirror_message(event)
        self._deliver_to_agent_comms(message)

    def format_mirror_message(self, event: Event) -> str:
        """Format a mailbox message event for the Agent Comms topic."""
        p = event.payload
        sender = p.get("from", "?")
        recipient = p.get("to", "?")
        msg_type = p.get("message_type", "?")
        summary = p.get("summary", "")

        header = f"{sender} -> {recipient}: {msg_type}"
        return f"{header}\n{summary}" if summary else header

    def _deliver_to_agent_comms(self, message: str) -> None:
        """Send to the Agent Comms topic."""
        if self._send_fn:
            self._send_fn(message)
            return

        try:
            config = json.loads(self._topics_path.read_text(encoding="utf-8"))
            chat_id = config.get("group_chat_id", "")
            thread_id = str(config.get("topics", {}).get("agent_comms", {}).get("thread_id", ""))
            if not chat_id or not thread_id:
                return

            from cron.scheduler import _deliver_result
            _deliver_result(
                {"deliver": f"telegram:{chat_id}:{thread_id}", "id": "event-bus", "name": "event-bus"},
                message,
            )
        except Exception as e:
            logger.error("TelegramMirror delivery failed: %s", e)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_telegram_mirror.py -v`

Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/subscribers/telegram_mirror.py tests/events/test_telegram_mirror.py
git commit -m "feat(events): add TelegramMirror — shadow-copies mailbox messages to Agent Comms"
```

---

## Task 13: Telegram Setup Script

**Files:**
- Create: `scripts/hermes_telegram_setup.py`

- [ ] **Step 1: Create setup script**

Create `scripts/hermes_telegram_setup.py`:

```python
#!/usr/bin/env python3
"""One-time Telegram group setup for Hermes Event Bus notifications.

Usage:
    python scripts/hermes_telegram_setup.py --chat-id=-100XXXXXXXXXX

Requires:
    - @j4um_bot added to the group as admin
    - Group must have Topics/Forum mode enabled
    - TELEGRAM_BOT_TOKEN set in ~/.hermes/profiles/main/.env
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

logger = logging.getLogger(__name__)

TOPICS = [
    {"key": "alerts", "name": "Alerts & Actions", "icon_color": 0xFF0000},
    {"key": "scout", "name": "Scout / Discoveries", "icon_color": 0x0088FF},
    {"key": "matcher", "name": "Matcher / Scores", "icon_color": 0xFFCC00},
    {"key": "tailor_applier", "name": "Tailor & Applier", "icon_color": 0x00CC66},
    {"key": "tracker", "name": "Tracker / Pipeline", "icon_color": 0x9933FF},
    {"key": "digests", "name": "Digests & Summaries", "icon_color": 0xFFFFFF},
    {"key": "system", "name": "System Health", "icon_color": 0x999999},
    {"key": "agent_comms", "name": "Agent Comms", "icon_color": 0xFF8800},
]


def get_bot_token() -> str:
    """Load bot token from .env file."""
    env_path = Path.home() / ".hermes" / "profiles" / "main" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not found in .env or environment")
    return token


def telegram_api(token: str, method: str, **params) -> dict:
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = httpx.post(url, json=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data.get('description', data)}")
    return data["result"]


def main():
    parser = argparse.ArgumentParser(description="Set up Telegram forum topics for Hermes")
    parser.add_argument("--chat-id", required=True, help="Telegram group chat ID (e.g., -100XXXXXXXXXX)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    token = get_bot_token()
    chat_id = args.chat_id

    # Verify bot has access
    logger.info("Verifying bot access to group %s...", chat_id)
    try:
        chat = telegram_api(token, "getChat", chat_id=chat_id)
        logger.info("Group: %s (type: %s)", chat.get("title", "?"), chat.get("type", "?"))
    except RuntimeError as e:
        logger.error("Cannot access group: %s", e)
        logger.error("Make sure @j4um_bot is added as admin and Topics are enabled.")
        sys.exit(1)

    # Create forum topics
    topics_config = {"group_chat_id": chat_id, "topics": {}}

    for topic_def in TOPICS:
        logger.info("Creating topic: %s...", topic_def["name"])
        try:
            result = telegram_api(
                token, "createForumTopic",
                chat_id=chat_id,
                name=topic_def["name"],
                icon_color=topic_def["icon_color"],
            )
            thread_id = result["message_thread_id"]
            topics_config["topics"][topic_def["key"]] = {
                "thread_id": thread_id,
                "name": topic_def["name"],
            }
            logger.info("  Created: thread_id=%s", thread_id)
        except RuntimeError as e:
            logger.error("  Failed: %s", e)

    # Save topic registry
    from hermes_constants import get_hermes_home
    telegram_dir = get_hermes_home() / "telegram"
    telegram_dir.mkdir(parents=True, exist_ok=True)

    topics_path = telegram_dir / "topics.json"
    from datetime import datetime, timezone
    topics_config["created_at"] = datetime.now(timezone.utc).isoformat()
    topics_path.write_text(json.dumps(topics_config, indent=2), encoding="utf-8")
    logger.info("\nTopic registry saved to: %s", topics_path)

    # Create default verbosity config
    verbosity = {key: {"mode": "all"} for key in topics_config["topics"]}
    verbosity["system"] = {"mode": "digest_only"}
    verbosity["agent_comms"] = {"mode": "significant_only"}

    verbosity_path = telegram_dir / "verbosity.json"
    verbosity_path.write_text(json.dumps(verbosity, indent=2), encoding="utf-8")
    logger.info("Verbosity config saved to: %s", verbosity_path)

    # Create default quiet hours config
    notifications_dir = get_hermes_home() / "notifications"
    notifications_dir.mkdir(parents=True, exist_ok=True)
    quiet_config = {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
    }
    quiet_path = notifications_dir / "quiet_hours.json"
    quiet_path.write_text(json.dumps(quiet_config, indent=2), encoding="utf-8")
    logger.info("Quiet hours config saved to: %s", quiet_path)

    # Send test messages
    logger.info("\nSending test messages to each topic...")
    for key, topic in topics_config["topics"].items():
        try:
            telegram_api(
                token, "sendMessage",
                chat_id=chat_id,
                message_thread_id=topic["thread_id"],
                text=f"Hermes Event Bus connected. Topic: {topic['name']}",
            )
            logger.info("  %s: OK", topic["name"])
        except RuntimeError as e:
            logger.error("  %s: FAILED — %s", topic["name"], e)

    # Update .env with home channel
    env_path = Path.home() / ".hermes" / "profiles" / "main" / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if "TELEGRAM_HOME_CHANNEL" not in content:
            content += f"\nTELEGRAM_HOME_CHANNEL={chat_id}\n"
            env_path.write_text(content)
            logger.info("\nAdded TELEGRAM_HOME_CHANNEL=%s to .env", chat_id)

    logger.info("\nSetup complete! Restart the gateway to activate notifications.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add scripts/hermes_telegram_setup.py
git commit -m "feat(events): add Telegram group setup script for forum topics"
```

---

## Task 14: Gateway Integration

Wire the EventBus, producers, and subscribers into the gateway lifecycle.

**Files:**
- Modify: `gateway/run.py` (startup and shutdown hooks)

- [ ] **Step 1: Create gateway integration module**

Create `events/gateway_integration.py`:

```python
"""Gateway integration — wires EventBus, producers, and subscribers into the gateway lifecycle.

Called from gateway/run.py during startup and shutdown.  All components
run within the gateway process — no new daemons or threads.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

from events.bus import EventBus
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger
from events.subscribers.telegram_notifier import TelegramNotifier
from events.subscribers.whatsapp_escalator import WhatsAppEscalator
from events.subscribers.digest_composer import DigestComposer
from events.subscribers.memory_writer import MemoryWriter
from events.subscribers.telegram_mirror import TelegramMirror

logger = logging.getLogger(__name__)

_bus: Optional[EventBus] = None
_registry: Optional[SubscriberRegistry] = None
_health_monitor: Optional[GatewayHealthMonitor] = None
_mailbox_watcher: Optional[MailboxWatcher] = None
_subscriber_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def startup(adapters: Optional[Dict] = None) -> None:
    """Initialize EventBus, register all subscribers, start polling thread."""
    global _bus, _registry, _health_monitor, _mailbox_watcher, _subscriber_thread

    logger.info("EventBus: initializing communication layer...")

    _bus = EventBus()
    _registry = SubscriberRegistry()
    _health_monitor = GatewayHealthMonitor(_bus)
    _mailbox_watcher = MailboxWatcher(_bus)

    # Register subscribers
    _registry.register(AuditLogger(_bus))
    _registry.register(TelegramNotifier(_bus))
    _registry.register(WhatsAppEscalator(_bus))
    _registry.register(DigestComposer(_bus))
    _registry.register(MemoryWriter(_bus))
    _registry.register(TelegramMirror(_bus))

    _registry.startup_all()

    # Start subscriber polling thread
    _stop_event.clear()
    _subscriber_thread = threading.Thread(
        target=_subscriber_poll_loop,
        daemon=True,
        name="event-subscribers",
    )
    _subscriber_thread.start()

    logger.info("EventBus: %d subscribers registered, polling started",
                len(_registry.subscribers))


def shutdown() -> None:
    """Stop polling and clean up."""
    global _subscriber_thread
    _stop_event.set()
    if _subscriber_thread:
        _subscriber_thread.join(timeout=5)
        _subscriber_thread = None
    if _registry:
        _registry.shutdown_all()
    logger.info("EventBus: shutdown complete")


def get_bus() -> Optional[EventBus]:
    """Get the global EventBus instance (for use by CronEventEmitter)."""
    return _bus


def get_health_monitor() -> Optional[GatewayHealthMonitor]:
    """Get the health monitor (for gateway adapter health checks)."""
    return _health_monitor


def _subscriber_poll_loop() -> None:
    """Background thread that polls all subscribers at their configured intervals."""
    last_poll_times: Dict[str, float] = {}
    last_mailbox_scan: float = 0
    last_cleanup: float = 0

    while not _stop_event.is_set():
        now = time.monotonic()

        # Poll each subscriber at its own interval
        if _registry:
            for sub in _registry.subscribers:
                last = last_poll_times.get(sub.subscriber_id, 0)
                if now - last >= sub.poll_interval_seconds:
                    try:
                        sub.poll()
                    except Exception:
                        logger.exception("Subscriber poll failed: %s", sub.subscriber_id)
                    last_poll_times[sub.subscriber_id] = now

        # Scan mailbox every 60 seconds
        if _mailbox_watcher and now - last_mailbox_scan >= 60:
            try:
                _mailbox_watcher.scan()
            except Exception:
                logger.exception("Mailbox scan failed")
            last_mailbox_scan = now

        # Daily cleanup (every 24 hours)
        if _bus and now - last_cleanup >= 86400:
            try:
                _bus.cleanup(retention_days=30)
            except Exception:
                logger.exception("Event cleanup failed")
            last_cleanup = now

        _stop_event.wait(timeout=1)  # tick every 1 second
```

- [ ] **Step 2: Hook into gateway/run.py startup**

In `gateway/run.py`, after the line `await self.hooks.emit("gateway:startup", {...})` (around line 1964), add:

```python
            # Initialize Event Bus communication layer
            try:
                from events.gateway_integration import startup as eventbus_startup
                eventbus_startup(adapters=self.adapters)
            except Exception as e:
                logger.warning("EventBus initialization failed (non-fatal): %s", e)
```

In the shutdown sequence (around line 9699-9708), before MCP shutdown, add:

```python
    # Shutdown Event Bus
    try:
        from events.gateway_integration import shutdown as eventbus_shutdown
        eventbus_shutdown()
    except Exception:
        pass
```

- [ ] **Step 3: Run tests to verify no regressions**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/ tests/gateway/ -v --timeout=30 -x`

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add events/gateway_integration.py gateway/run.py
git commit -m "feat(events): wire EventBus and subscribers into gateway lifecycle"
```

---

## Task 15: Cron Job Delivery Migration & Legacy Cleanup

**Files:**
- Modify: `~/.hermes/cron/jobs.json` (update delivery targets)
- Modify: `~/.openclaw/cron/jobs.json` (disable all)

- [ ] **Step 1: Create migration script**

Create `scripts/migrate_cron_delivery.py`:

```python
#!/usr/bin/env python3
"""Migrate cron job delivery targets and disable OpenClaw legacy jobs.

Changes:
  1. All Hermes cron jobs: deliver → "local" (EventBus handles delivery)
  2. Delete jaum-daytime-relay job
  3. Disable all OpenClaw cron jobs
  4. Create jobflow-archiver job
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

HERMES_CRON = Path.home() / ".hermes" / "cron" / "jobs.json"
OPENCLAW_CRON = Path.home() / ".openclaw" / "cron" / "jobs.json"


def migrate_hermes_jobs():
    """Update Hermes cron jobs: deliver=local, remove daytime-relay."""
    if not HERMES_CRON.exists():
        print("Hermes cron/jobs.json not found, skipping")
        return

    jobs = json.loads(HERMES_CRON.read_text(encoding="utf-8"))
    modified = False

    # Remove jaum-daytime-relay
    original_count = len(jobs)
    jobs = [j for j in jobs if j.get("name") != "jaum-daytime-relay"]
    if len(jobs) < original_count:
        print("Removed jaum-daytime-relay")
        modified = True

    # Set all deliver fields to "local"
    for job in jobs:
        if job.get("deliver") not in ("local", None):
            old = job.get("deliver", "unset")
            job["deliver"] = "local"
            print(f"  {job.get('name', job['id'])}: deliver {old} → local")
            modified = True

    # Add jobflow-archiver if not exists
    if not any(j.get("name") == "jobflow-archiver" for j in jobs):
        import uuid
        archiver = {
            "id": uuid.uuid4().hex[:12],
            "name": "jobflow-archiver",
            "prompt": (
                "Review pipeline.json. Archive any job that has been stale "
                "(no status change) for 30+ days. NEVER archive jobs in "
                "interviewing, offer, or negotiation stages. Log archived "
                "jobs to workspace/archived.jsonl with reason and date."
            ),
            "skills": [],
            "skill": None,
            "model": None,
            "provider": None,
            "base_url": None,
            "script": None,
            "schedule": {"kind": "cron", "expr": "0 2 * * 0", "display": "Sunday 2am ET"},
            "schedule_display": "0 2 * * 0",
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "next_run_at": None,  # Will be computed on first tick
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "deliver": "local",
            "origin": None,
            "last_delivery_error": None,
            "consecutive_errors": 0,
        }
        jobs.append(archiver)
        print("Added jobflow-archiver (Sunday 2am)")
        modified = True

    if modified:
        HERMES_CRON.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nHermes jobs updated: {len(jobs)} jobs")
    else:
        print("No Hermes changes needed")


def disable_openclaw_jobs():
    """Disable all OpenClaw cron jobs."""
    if not OPENCLAW_CRON.exists():
        print("\nOpenClaw cron/jobs.json not found, skipping")
        return

    jobs = json.loads(OPENCLAW_CRON.read_text(encoding="utf-8"))
    count = 0
    for job in jobs:
        if job.get("enabled", False):
            job["enabled"] = False
            job["paused_at"] = datetime.now(timezone.utc).isoformat()
            job["paused_reason"] = "Migrated to Hermes EventBus — disabled by migration script"
            count += 1

    OPENCLAW_CRON.write_text(json.dumps(jobs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nOpenClaw: disabled {count} jobs")


if __name__ == "__main__":
    print("=== Hermes Communication Layer Migration ===\n")
    migrate_hermes_jobs()
    disable_openclaw_jobs()
    print("\nMigration complete. Restart the gateway to activate the EventBus.")
```

- [ ] **Step 2: Commit (do NOT run the script yet — it modifies live config)**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add scripts/migrate_cron_delivery.py
git commit -m "feat(events): add cron delivery migration script — local delivery + OpenClaw cleanup"
```

- [ ] **Step 3: Run the migration script**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
python scripts/migrate_cron_delivery.py
```

Expected: Reports changes made to both jobs.json files.

- [ ] **Step 4: Verify migration**

```bash
python -c "
import json
from pathlib import Path
h = json.loads((Path.home() / '.hermes/cron/jobs.json').read_text())
print(f'Hermes: {len(h)} jobs')
for j in h: print(f'  {j[\"name\"]}: deliver={j.get(\"deliver\")}, enabled={j.get(\"enabled\")}')
print()
o = json.loads((Path.home() / '.openclaw/cron/jobs.json').read_text())
enabled = [j for j in o if j.get('enabled')]
print(f'OpenClaw: {len(o)} total, {len(enabled)} enabled')
"
```

Expected: All Hermes jobs have `deliver=local`, no `jaum-daytime-relay`, `jobflow-archiver` present. All OpenClaw jobs disabled.

---

## Task 16: Full Integration Test

**Files:**
- Create: `tests/events/test_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/events/test_integration.py`:

```python
"""Integration test — end-to-end event flow from emit to subscriber consumption."""

import json
from pathlib import Path

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.cron_emitter import CronEventEmitter
from events.producers.health_monitor import GatewayHealthMonitor
from events.producers.mailbox_watcher import MailboxWatcher
from events.subscribers.base import SubscriberRegistry
from events.subscribers.audit_logger import AuditLogger


@pytest.fixture
def setup(tmp_path):
    """Create a complete event bus setup with producers and subscribers."""
    db_path = tmp_path / "events" / "event_bus.db"
    bus = EventBus(db_path=db_path)
    return {
        "bus": bus,
        "tmp_path": tmp_path,
        "emitter": CronEventEmitter(bus),
        "health": GatewayHealthMonitor(bus),
    }


class TestEndToEnd:
    def test_cron_emit_to_audit_log(self, setup):
        bus = setup["bus"]
        emitter = setup["emitter"]
        tmp_path = setup["tmp_path"]

        audit_path = tmp_path / "events" / "audit.jsonl"
        audit = AuditLogger(bus, audit_path=audit_path)

        # Producer emits
        emitter.on_job_started("j1", "jobflow-scout", "0 8 * * *")
        emitter.on_job_completed("j1", "jobflow-scout", True, 120.0, "Found 8 jobs")

        # Subscriber consumes
        audit.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["event_type"] == "cron_started"
        assert json.loads(lines[1])["event_type"] == "cron_completed"

    def test_health_state_change_flow(self, setup):
        bus = setup["bus"]
        health = setup["health"]
        tmp_path = setup["tmp_path"]

        audit_path = tmp_path / "events" / "audit.jsonl"
        audit = AuditLogger(bus, audit_path=audit_path)

        health.report_health("telegram", healthy=True)
        health.report_health("telegram", healthy=False)
        health.report_health("telegram", healthy=False)  # no event
        health.report_health("telegram", healthy=True)

        audit.poll()

        lines = audit_path.read_text().strip().split("\n")
        assert len(lines) == 3  # up, down, up (no duplicate down)

    def test_mailbox_to_event_flow(self, setup):
        bus = setup["bus"]
        tmp_path = setup["tmp_path"]

        mailbox = tmp_path / "mailbox" / "main" / "inbox"
        mailbox.mkdir(parents=True)

        msg = {"type": "SCORE_RESULT", "from": "matcher", "to": "main",
               "payload": {"score": 8.5}}
        (mailbox / "20260415T120000Z_SCORE_RESULT_matcher.json").write_text(
            json.dumps(msg), encoding="utf-8",
        )

        watcher = MailboxWatcher(bus, mailbox_root=tmp_path / "mailbox")
        watcher.scan()

        events = bus.query(event_type=EventType.MAILBOX_MESSAGE)
        assert len(events) == 1
        assert events[0].payload["message_type"] == "SCORE_RESULT"

    def test_full_registry_poll(self, setup):
        bus = setup["bus"]
        emitter = setup["emitter"]
        tmp_path = setup["tmp_path"]

        registry = SubscriberRegistry()
        audit = AuditLogger(bus, audit_path=tmp_path / "events" / "audit.jsonl")
        registry.register(audit)

        emitter.on_job_started("j1", "scout", "0 8 * * *")
        emitter.on_job_completed("j1", "scout", True, 60.0, "5 jobs")
        emitter.on_job_completed("j2", "matcher", False, 10.0, error="timeout", consecutive_errors=3)

        results = registry.poll_all()
        assert results["audit-logger"] == 4  # started + completed + failed + consecutive
```

- [ ] **Step 2: Run integration tests**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/test_integration.py -v`

Expected: All tests PASS

- [ ] **Step 3: Run full event test suite**

Run: `cd C:/Users/diego/Usersdiegohermes-agent-src && python -m pytest tests/events/ -v`

Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
cd C:/Users/diego/Usersdiegohermes-agent-src
git add tests/events/test_integration.py
git commit -m "test(events): add end-to-end integration tests for EventBus pipeline"
```

---

## Summary

### Implementation order (dependency chain):

```
Task 1:  Event Schema (foundation)
Task 2:  EventBus Core (depends on schema)
Task 3:  BaseSubscriber + Registry (depends on bus)
Task 4:  AuditLogger (validates subscriber pattern)
Task 5:  CronEventEmitter + cron hooks (depends on bus)
Task 6:  GatewayHealthMonitor (depends on bus)
Task 7:  MailboxWatcher (depends on bus)
Task 8:  TelegramNotifier (depends on subscriber base)
Task 9:  WhatsAppEscalator (depends on subscriber base)
Task 10: DigestComposer (depends on subscriber base)
Task 11: MemoryWriter (depends on subscriber base)
Task 12: TelegramMirror (depends on subscriber base)
Task 13: Telegram Setup Script (standalone)
Task 14: Gateway Integration (depends on all above)
Task 15: Cron Migration + Legacy Cleanup (depends on gateway integration)
Task 16: Integration Tests (validates everything)
```

### Parallelizable tasks:
- Tasks 5, 6, 7 (producers) can run in parallel
- Tasks 8, 9, 10, 11, 12 (subscribers) can run in parallel
- Task 13 (setup script) is independent

### Files created: 22 new files
### Files modified: 3 existing files (cron/scheduler.py, cron/jobs.py, gateway/run.py)
### Tests: 13 test files covering all components
