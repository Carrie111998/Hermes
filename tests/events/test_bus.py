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
