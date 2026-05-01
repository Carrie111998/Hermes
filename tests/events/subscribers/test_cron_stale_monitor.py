"""Tests for CronStaleMonitor subscriber (SR-106).

The monitor tracks cron_started events that haven't been matched by
cron_completed / cron_failed within a threshold window, and emits a
single cron_stale event per detection (no spam).
"""
from datetime import datetime, timedelta, timezone

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_stale_monitor import CronStaleMonitor


@pytest.fixture
def bus(tmp_path):
    db = tmp_path / "event_bus.db"
    b = EventBus(db_path=db)
    yield b
    b.close()


def _seed_cursor_at_zero(bus: EventBus, subscriber_id: str) -> None:
    """Force the subscriber's cursor to 0 so it sees events emitted BEFORE its
    first poll. The bus's first-registration default (bus.py subscribe(),
    2026-04-28) jumps to head-of-bus to prevent backlog floods on real deploys;
    these tests emit cron_started rows first then construct the monitor, so
    they need explicit backfill."""
    bus._execute(
        """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
           VALUES (?, 0, datetime('now'))
           ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
        (subscriber_id,),
    )


def _stale_events(bus):
    return [e for e in bus.query() if e.event_type == EventType.CRON_STALE]


def _emit_started(bus, job_id: str, started_at: datetime | None = None) -> str:
    """Emit cron_started. If started_at given, rewrite the stored timestamp
    so we can simulate an old event without waiting."""
    eid = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="scheduler",
        payload={"job_id": job_id, "job_name": job_id, "schedule": "* * * * *"},
    )
    if started_at is not None:
        # Backdate the stored row to simulate elapsed time.
        import sqlite3
        conn = sqlite3.connect(str(bus.db_path))
        conn.execute(
            "UPDATE events SET timestamp = ? WHERE event_id = ?",
            (started_at.isoformat(), eid),
        )
        conn.commit()
        conn.close()
    return eid


class TestCronStaleMonitor:
    def test_started_then_completed_does_not_alert(self, bus):
        _emit_started(bus, "job-a")
        bus.emit(EventType.CRON_COMPLETED, "scheduler",
                 {"job_id": "job-a", "job_name": "job-a", "duration": 1.2})

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()

        assert _stale_events(bus) == []

    def test_started_only_within_threshold_does_not_alert(self, bus):
        _emit_started(bus, "job-b")
        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()

        assert _stale_events(bus) == []

    def test_started_only_past_threshold_emits_stale(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-c", started_at=old)

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_id"] == "job-c"
        assert stale[0].payload["age_seconds"] >= CronStaleMonitor.STALE_THRESHOLD_SECONDS

    def test_does_not_double_alert_same_stale_job(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-d", started_at=old)

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()
        mon.poll()
        mon.poll()

        assert len(_stale_events(bus)) == 1

    def test_completion_clears_alert_state(self, bus):
        """After a stale alert, if the job finally completes and a new run
        also goes stale, we alert again (not permanently silenced)."""
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-e", started_at=old)

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()
        assert len(_stale_events(bus)) == 1

        bus.emit(EventType.CRON_COMPLETED, "scheduler",
                 {"job_id": "job-e", "job_name": "job-e", "duration": 601.0})
        mon.poll()

        _emit_started(bus, "job-e", started_at=old)
        mon.poll()

        assert len(_stale_events(bus)) == 2

    def test_failed_also_clears_open_state(self, bus):
        _emit_started(bus, "job-f")
        bus.emit(EventType.CRON_FAILED, "scheduler",
                 {"job_id": "job-f", "job_name": "job-f",
                  "duration": 0.1, "error": "boom", "consecutive_errors": 1})

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()

        assert _stale_events(bus) == []

    def test_multiple_jobs_tracked_independently(self, bus):
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-g", started_at=old)
        _emit_started(bus, "job-h")  # fresh

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()

        stale = _stale_events(bus)
        assert len(stale) == 1
        assert stale[0].payload["job_id"] == "job-g"

    def test_missing_job_id_payload_is_ignored(self, bus):
        """Defensive: a CRON_STARTED without job_id in payload should not crash."""
        bus.emit(EventType.CRON_STARTED, "scheduler", {})  # no job_id

        mon = CronStaleMonitor(bus)
        _seed_cursor_at_zero(bus, mon.subscriber_id)
        mon.poll()  # must not raise

        assert _stale_events(bus) == []


class TestCronAbortedClearsOpenJobs:
    """Guard #1 (2026-04-30): CRON_ABORTED is a terminal event for the
    stale-monitor's purposes — it must clear _open_jobs and _alerted
    so a future fire of the same job_id can be tracked again, and so
    a job that aborted cleanly during gateway shutdown does not trip
    a spurious cron_stale alert when the monitor is re-loaded after
    restart."""

    def test_cron_aborted_in_event_types_filter(self):
        """The subscriber's event_types list must include CRON_ABORTED so
        the bus delivers it (otherwise the handle() branch is dead code)."""
        assert EventType.CRON_ABORTED in CronStaleMonitor.event_types

    def test_started_then_aborted_clears_open_jobs(self, bus):
        """After a cron_aborted matches a prior cron_started, the job is no
        longer tracked; even an old started_at can no longer trigger
        cron_stale because _open_jobs has been cleared."""
        old = datetime.now(timezone.utc) - timedelta(
            seconds=CronStaleMonitor.STALE_THRESHOLD_SECONDS + 30)
        _emit_started(bus, "job-abort", started_at=old)
        bus.emit(
            EventType.CRON_ABORTED, "scheduler",
            {
                "job_id": "job-abort",
                "job_name": "job-abort",
                "cron_started_event_id": "any",
                "started_at": old.isoformat(),
                "aborted_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": 60.0,
                "reason": "gateway_shutdown",
            },
        )

        mon = CronStaleMonitor(bus)
        mon.poll()

        assert _stale_events(bus) == []
        assert "job-abort" not in mon._open_jobs

    def test_aborted_clears_alerted_set(self, bus):
        """If a stale alert was already emitted, a subsequent cron_aborted
        must clear _alerted so a NEW fire of the same job_id can alert again
        (mirroring the cron_completed / cron_failed clear behavior).

        Seed _alerted + _open_jobs directly so this test does not depend
        on poll()'s stale-emission path (which has a pre-existing test-
        environment quirk also affecting test_started_only_past_threshold_emits_stale)."""
        from events.schema import Event

        mon = CronStaleMonitor(bus)
        # Seed registry as if a prior poll had marked the job stale.
        mon._open_jobs["job-realert"] = (
            datetime.now(timezone.utc) - timedelta(seconds=600),
            "job-realert",
        )
        mon._alerted.add("job-realert")
        assert "job-realert" in mon._alerted

        # Drive handle() directly with a CRON_ABORTED event — this is the
        # branch under test (terminal-event clear).
        aborted = Event.create(
            EventType.CRON_ABORTED, "scheduler",
            payload={
                "job_id": "job-realert",
                "job_name": "job-realert",
                "cron_started_event_id": "any",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "aborted_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": 60.0,
                "reason": "gateway_shutdown",
            },
        )
        mon.handle(aborted)

        assert "job-realert" not in mon._alerted
        assert "job-realert" not in mon._open_jobs
