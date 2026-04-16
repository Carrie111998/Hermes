"""Tests for events.bus -- SQLite-backed EventBus."""

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


class TestSchemaMigration:
    def test_migrates_legacy_db_without_status_column(self, tmp_path):
        """EventBus must upgrade pre-0.9.1 databases that lack the status column."""
        import sqlite3

        db_path = tmp_path / "legacy" / "event_bus.db"
        db_path.parent.mkdir(parents=True)

        # Create a legacy DB with the old schema (no status column, old index)
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE events (
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
            CREATE INDEX idx_events_type_ts ON events (event_type, created_at);
        """)
        # Insert some legacy data
        conn.execute(
            "INSERT INTO events (event_id, event_type, source, timestamp, priority) "
            "VALUES ('legacy-1', 'cron_started', 'old', '2026-01-01T00:00:00Z', 'low')"
        )
        conn.commit()
        conn.close()

        # Opening with the new EventBus should migrate the schema in place
        bus = EventBus(db_path=db_path)

        # Verify the status column now exists
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert "status" in cols
        # Verify the new index was created
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(events)")}
        assert "idx_events_type_status_ts" in indexes
        # Legacy row is preserved
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 1
        conn.close()

        # New events can be emitted against the migrated DB
        event_id = bus.emit(EventType.CRON_COMPLETED, "new", {"ok": True})
        assert event_id
        assert len(bus.query()) == 2  # legacy + new
        bus.close()


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
