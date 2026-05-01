"""Tests for events.producers.cron_trigger_emitter."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.cron_trigger_emitter import emit_cron_triggered


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def test_emit_basic(bus):
    event_id = emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="sentinel-vip-morning",
        caller="hermes_cli:cron_run",
        reason="investigation 2026-04-30",
        previous_next_run_at="2026-05-01T09:00:00+00:00",
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    assert event_id

    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    e = events[0]
    assert e.source == "sentinel-vip-morning"
    assert e.priority is Priority.LOW
    assert e.job_id == "abc123"
    assert e.payload["caller"] == "hermes_cli:cron_run"
    assert e.payload["reason"] == "investigation 2026-04-30"
    assert e.payload["job_name"] == "sentinel-vip-morning"
    assert e.payload["previous_next_run_at"] == "2026-05-01T09:00:00+00:00"
    assert e.payload["new_next_run_at"] == "2026-04-30T14:34:00+00:00"


def test_emit_anonymous_caller_omits_field(bus):
    emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="job",
        caller=None,
        reason=None,
        previous_next_run_at=None,
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    assert events[0].payload["caller"] is None
    assert events[0].payload["reason"] is None
    assert events[0].payload["previous_next_run_at"] is None


def test_emit_swallows_bus_failure(bus, monkeypatch, caplog):
    """A broken bus must NOT propagate — trigger_job must keep working."""
    def boom(*args, **kwargs):
        raise RuntimeError("bus is dead")

    monkeypatch.setattr(bus, "emit", boom)

    result = emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="job",
        caller="hermes_cli:cron_run",
        reason=None,
        previous_next_run_at=None,
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    assert result is None
    assert "cron_trigger_emitter" in caplog.text or "emit failed" in caplog.text.lower()
