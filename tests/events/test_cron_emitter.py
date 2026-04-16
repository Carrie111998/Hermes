"""Tests for events.producers.cron_emitter -- CronEventEmitter."""

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
