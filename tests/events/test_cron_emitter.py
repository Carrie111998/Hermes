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


class TestSkippedDuplicate:
    """Cron same-job concurrency guard -- closes 2026-04-30 sentinel triple-fire
    (canonical event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e)."""

    def test_emit_skipped_duplicate_concurrent(self, emitter, bus):
        emitter.on_job_skipped_duplicate(
            job_id="092f4ed7657c",
            job_name="sentinel-vip-morning",
            prior_cron_started_event_id="4edcb4b1-aa07-4dbb-b799-8af167d4f92e",
            prior_elapsed_seconds=12.4,
            reason="concurrent_fire_blocked",
        )

        events = bus.query(event_type=EventType.CRON_SKIPPED_DUPLICATE)
        assert len(events) == 1
        evt = events[0]
        assert evt.source == "sentinel-vip-morning"
        assert evt.priority == Priority.LOW
        assert evt.payload["job_id"] == "092f4ed7657c"
        assert evt.payload["job_name"] == "sentinel-vip-morning"
        assert (
            evt.payload["prior_cron_started_event_id"]
            == "4edcb4b1-aa07-4dbb-b799-8af167d4f92e"
        )
        assert evt.payload["prior_elapsed_seconds"] == 12.4
        assert evt.payload["reason"] == "concurrent_fire_blocked"

    def test_emit_skipped_duplicate_exceeded_timeout(self, emitter, bus):
        emitter.on_job_skipped_duplicate(
            job_id="092f4ed7657c",
            job_name="sentinel-vip-morning",
            prior_cron_started_event_id=None,
            prior_elapsed_seconds=2100.0,
            reason="prior_fire_exceeded_timeout",
        )

        events = bus.query(event_type=EventType.CRON_SKIPPED_DUPLICATE)
        assert len(events) == 1
        assert events[0].payload["reason"] == "prior_fire_exceeded_timeout"
        assert events[0].payload["prior_cron_started_event_id"] is None


def test_cron_emitter_has_no_regex_domain_parser():
    import events.producers.cron_emitter as ce
    src = open(ce.__file__, encoding="utf-8").read()
    assert "_DOMAIN_PATTERNS" not in src, (
        "Regex domain parser should be retired. Domain events come from "
        "MailboxTranslator consuming mailbox_message events."
    )
    assert not hasattr(ce.CronEventEmitter, "_parse_output_for_domain_events"), (
        "_parse_output_for_domain_events should be removed; MailboxTranslator "
        "handles domain event emission."
    )
