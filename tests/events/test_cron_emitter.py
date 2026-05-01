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

    def test_emit_skipped(self, emitter, bus):
        emitter.on_job_skipped(
            job_id="job-1",
            job_name="sentinel-vip-evening",
            missed_at="2026-04-29T23:00:00+00:00",
            missed_seconds=14400,
            schedule_kind="cron",
            reason="default_period_cap",
        )

        events = bus.query(event_type=EventType.CRON_SKIPPED)
        assert len(events) == 1
        assert events[0].source == "sentinel-vip-evening"
        assert events[0].priority == Priority.HIGH
        payload = events[0].payload
        assert payload["job_id"] == "job-1"
        assert payload["job_name"] == "sentinel-vip-evening"
        assert payload["missed_at"] == "2026-04-29T23:00:00+00:00"
        assert payload["missed_seconds"] == 14400
        assert payload["schedule_kind"] == "cron"
        assert payload["reason"] == "default_period_cap"


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


class TestOnJobAborted:
    """CronEventEmitter.on_job_aborted (Guard #1, 2026-04-30) — emits
    CRON_ABORTED. Deliberately does NOT feed FailureClusterDetector:
    abort is gateway-fault, not agent-fault, so it must not trip
    same-source cluster alerts."""

    def test_emits_cron_aborted_with_full_payload(self, emitter, bus):
        emitter.on_job_aborted(
            job_id="092f4ed7657c",
            job_name="sentinel-vip-morning",
            cron_started_event_id="4edcb4b1-aa07-4dbb-b799-8af167d4f92e",
            started_at="2026-04-30T14:49:52+00:00",
            aborted_at="2026-04-30T14:56:43+00:00",
            elapsed_seconds=411.0,
            reason="gateway_shutdown",
        )

        events = bus.query(event_type=EventType.CRON_ABORTED)
        assert len(events) == 1
        ev = events[0]
        assert ev.source == "sentinel-vip-morning"
        assert ev.priority == Priority.HIGH
        assert ev.payload == {
            "job_id": "092f4ed7657c",
            "job_name": "sentinel-vip-morning",
            "cron_started_event_id": "4edcb4b1-aa07-4dbb-b799-8af167d4f92e",
            "started_at": "2026-04-30T14:49:52+00:00",
            "aborted_at": "2026-04-30T14:56:43+00:00",
            "elapsed_seconds": 411.0,
            "reason": "gateway_shutdown",
        }

    def test_emits_for_wallclock_timeout_reason(self, emitter, bus):
        """Reason field passes through verbatim so a future wallclock-path
        wiring can call this helper without re-shaping."""
        emitter.on_job_aborted(
            job_id="job-x",
            job_name="hung-job",
            cron_started_event_id=None,
            started_at="2026-04-30T13:00:00+00:00",
            aborted_at="2026-04-30T13:30:00+00:00",
            elapsed_seconds=1800.0,
            reason="wallclock_timeout",
        )

        events = bus.query(event_type=EventType.CRON_ABORTED)
        assert len(events) == 1
        assert events[0].payload["reason"] == "wallclock_timeout"
        assert events[0].payload["cron_started_event_id"] is None

    def test_does_not_record_failure_cluster(self, emitter):
        """Cron_aborted is gateway-fault. If on_job_aborted fed the
        cluster detector, a single gateway restart could trip a spurious
        agent_failure_cluster for whichever agent was in flight."""
        from unittest.mock import MagicMock

        emitter._cluster_detector = MagicMock()  # spy

        emitter.on_job_aborted(
            job_id="any",
            job_name="any-cron",
            cron_started_event_id=None,
            started_at="2026-04-30T15:00:00+00:00",
            aborted_at="2026-04-30T15:00:01+00:00",
            elapsed_seconds=1.0,
            reason="gateway_shutdown",
        )

        emitter._cluster_detector.record.assert_not_called()


class TestWallclockTimeoutFlowsThroughCronFailed:
    """Pin the design decision (2026-04-30 trade-off addendum) that a
    wallclock-timeout failure surfaces as CRON_FAILED via
    ``on_job_completed(success=False)``, NOT as CRON_ABORTED via
    ``on_job_aborted``.

    This is the emitter-level counterpart to
    ``tests/cron/test_scheduler.py::TestWallclockTimeoutEmitsCronFailedNotAborted``,
    which pins the scheduler's decision not to invoke ``on_job_aborted``
    for the wallclock case. Together they prevent the symmetry argument
    ("every cron_started should pair with cron_aborted on scheduler-kill")
    from quietly silencing the cluster-detector / consecutive-failure
    operator alerts that today's cron_failed path provides.

    See ``docs/superpowers/specs/2026-04-30-cron-aborted-wallclock-trade-off.md``
    for the full rationale.
    """

    def test_wallclock_error_emits_cron_failed_and_feeds_cluster_detector(
        self, emitter, bus
    ):
        """Calling ``on_job_completed(success=False, error=<wallclock-text>)``
        must emit CRON_FAILED (not CRON_ABORTED) and feed
        FailureClusterDetector. ``on_job_aborted`` deliberately skips the
        detector (see ``test_does_not_record_failure_cluster`` above) --
        switching wallclock to that path would silence repeated-wedge
        clustering. This test pins the contract that prevents that drift.
        """
        from unittest.mock import MagicMock

        # Spy on the cluster detector so we can assert it's invoked
        # (unlike the on_job_aborted path).
        emitter._cluster_detector = MagicMock()
        emitter._cluster_detector.record.return_value = None

        wallclock_error = (
            "TimeoutError: Cron job 'hung-cron' exceeded wall-clock limit "
            "1800s (elapsed 1801s) -- last activity: api_call_streaming"
        )

        emitter.on_job_completed(
            job_id="job-x",
            job_name="hung-cron",
            success=False,
            duration=1801.0,
            error=wallclock_error,
            consecutive_errors=1,
        )

        # Behavior pin #1: emits CRON_FAILED, NOT CRON_ABORTED.
        failed_events = bus.query(event_type=EventType.CRON_FAILED)
        aborted_events = bus.query(event_type=EventType.CRON_ABORTED)
        assert len(failed_events) == 1, (
            "wallclock failure must emit cron_failed via on_job_completed; "
            f"got {len(failed_events)} cron_failed events"
        )
        assert len(aborted_events) == 0, (
            "wallclock failure must NOT emit cron_aborted; that event is "
            "reserved for gateway-fault scenarios. See "
            "docs/superpowers/specs/2026-04-30-cron-aborted-wallclock-trade-off.md"
        )

        # Behavior pin #2: error_text is preserved in the cron_failed
        # payload so downstream classification + operator-readable
        # diagnostics work.
        assert "wall-clock" in failed_events[0].payload["error"].lower()

        # Behavior pin #3: cluster detector is fed (unlike on_job_aborted's
        # deliberate skip). This is the operator-page signal -- 3 consecutive
        # wallclock failures from the same source can trip
        # agent_failure_cluster -- that we'd lose by re-routing to
        # cron_aborted.
        emitter._cluster_detector.record.assert_called_once()
        record_kwargs = emitter._cluster_detector.record.call_args.kwargs
        assert record_kwargs["success"] is False
        assert "wall-clock" in record_kwargs["error_text"].lower()
