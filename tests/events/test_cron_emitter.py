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


class TestOutputParsing:
    """Tests for _parse_output_for_domain_events -- regex-based signal extraction."""

    def test_found_jobs_emits_job_discovered(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-1",
            job_name="jobflow-scout",
            success=True,
            duration=30.0,
            output_summary="Found 12 new jobs on Indeed and LinkedIn",
        )

        events = bus.query(event_type=EventType.JOB_DISCOVERED)
        assert len(events) == 1
        assert events[0].payload["count"] == 12
        assert events[0].payload["_parsed_from"] == "jobflow-scout"

    def test_scored_emits_job_scored(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-2",
            job_name="jobflow-matcher",
            success=True,
            duration=15.0,
            output_summary="scored 8.5/10 for Senior Engineer at Google.",
        )

        events = bus.query(event_type=EventType.JOB_SCORED)
        assert len(events) == 1
        assert events[0].payload["score"] == 8.5
        assert "Senior Engineer at Google" in events[0].payload["title"]

    def test_submitted_emits_application_submitted(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-3",
            job_name="jobflow-applier",
            success=True,
            duration=20.0,
            output_summary="submitted to Acme Corp.",
        )

        events = bus.query(event_type=EventType.APPLICATION_SUBMITTED)
        assert len(events) == 1
        assert events[0].payload["company"] == "Acme Corp"

    def test_no_domain_signals_emits_nothing(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-4",
            job_name="jobflow-scout",
            success=True,
            duration=5.0,
            output_summary="Checked mailbox. Nothing new.",
        )

        # Should only have the cron_completed event, no domain events
        all_events = bus.query()
        domain_types = {
            EventType.JOB_DISCOVERED, EventType.JOB_SCORED,
            EventType.JOB_HIGH_SCORE, EventType.APPLICATION_SUBMITTED,
            EventType.STAGE_TRANSITION, EventType.TAILOR_COMPLETED,
            EventType.APPLICATION_READY, EventType.JOB_VIP_DISCOVERED,
        }
        domain_events = [e for e in all_events if e.event_type in domain_types]
        assert len(domain_events) == 0

    def test_multiple_signals_emits_one_per_type(self, emitter, bus):
        emitter.on_job_completed(
            job_id="job-5",
            job_name="jobflow-pipeline",
            success=True,
            duration=60.0,
            output_summary=(
                "Found 5 new jobs. "
                "scored 7.2/10 for Data Analyst at Meta. "
                "submitted to Acme Corp."
            ),
        )

        discovered = bus.query(event_type=EventType.JOB_DISCOVERED)
        scored = bus.query(event_type=EventType.JOB_SCORED)
        submitted = bus.query(event_type=EventType.APPLICATION_SUBMITTED)

        assert len(discovered) == 1
        assert len(scored) == 1
        assert len(submitted) == 1

        # Verify no duplicates: each type appears exactly once
        all_events = bus.query()
        domain_types = {
            EventType.JOB_DISCOVERED, EventType.JOB_SCORED,
            EventType.APPLICATION_SUBMITTED,
        }
        domain_events = [e for e in all_events if e.event_type in domain_types]
        assert len(domain_events) == 3
