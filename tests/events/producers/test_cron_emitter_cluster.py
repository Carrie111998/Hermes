"""Tests for failure-cluster wiring inside CronEventEmitter.

The emitter wraps the FailureClusterDetector and emits AGENT_FAILURE_CLUSTER
into the bus when 3 consecutive same-type failures are recorded for a
source.  This is the universal funnel — every cron-driven agent run flows
through on_job_completed.
"""

from pathlib import Path

import pytest

from events.bus import EventBus
from events.producers.cron_emitter import CronEventEmitter
from events.schema import EventType


@pytest.fixture
def bus(tmp_path):
    db_path = tmp_path / "events" / "event_bus.db"
    return EventBus(db_path=db_path)


@pytest.fixture
def emitter(bus, tmp_path, monkeypatch):
    """CronEventEmitter with detector state isolated to tmp_path."""
    state_path = tmp_path / "events" / "failure_cluster_state.json"
    monkeypatch.setattr(
        "events.producers.cron_emitter.failure_cluster_state_path",
        lambda: state_path,
    )
    return CronEventEmitter(bus)


def _fail(emitter, name, error, consecutive):
    emitter.on_job_completed(
        job_id=f"job-{name}",
        job_name=name,
        success=False,
        duration=1.0,
        error=error,
        consecutive_errors=consecutive,
    )


class TestEmitterClusterEmission:
    def test_single_failure_emits_no_cluster(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 1)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_three_same_type_emits_cluster(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 1)
        _fail(emitter, "scout", "timed out", 2)
        _fail(emitter, "scout", "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        evt = clusters[0]
        assert evt.source == "scout"
        assert evt.payload["failure_type"] == "timeout"
        assert evt.payload["count"] == 3

    def test_three_different_types_no_cluster(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 1)
        _fail(emitter, "scout", "captcha", 2)
        _fail(emitter, "scout", "401 Unauthorized", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []

    def test_success_clears_window(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 1)
        _fail(emitter, "scout", "timeout", 2)
        emitter.on_job_completed(
            job_id="job-scout", job_name="scout", success=True,
            duration=1.0, output_summary="ok",
        )
        _fail(emitter, "scout", "timeout", 1)
        _fail(emitter, "scout", "timeout", 2)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert clusters == []  # window was cleared by the success

    def test_existing_cron_failed_still_emits(self, bus, emitter):
        """Adding the cluster emission must not regress existing events."""
        _fail(emitter, "scout", "timeout", 1)
        cron_failed = bus.query(event_type=EventType.CRON_FAILED)
        assert len(cron_failed) == 1

    def test_existing_cron_failed_consecutive_still_emits(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 3)
        consec = bus.query(event_type=EventType.CRON_FAILED_CONSECUTIVE)
        assert len(consec) == 1

    def test_cluster_payload_shape(self, bus, emitter):
        _fail(emitter, "scout", "timeout", 1)
        _fail(emitter, "scout", "timeout", 2)
        _fail(emitter, "scout", "timeout", 3)
        evt = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)[0]
        assert set(evt.payload.keys()) >= {
            "source", "failure_type", "count", "first_seen", "last_seen",
        }


class TestPerAgentSmoke:
    """Parameterized smoke test — every Hermes agent source must be able
    to trigger a cluster.  Closes the brief's per-agent wiring requirement
    without changing any per-agent code.
    """

    AGENT_SOURCES = [
        "scout", "sentinel", "matcher", "tailor", "applier",
        "tracker", "notifier", "cv-handler", "devflow", "main",
    ]

    @pytest.mark.parametrize("source", AGENT_SOURCES)
    def test_each_agent_emits_cluster_on_three_same_type_failures(
        self, source, bus, emitter,
    ):
        _fail(emitter, source, "timeout", 1)
        _fail(emitter, source, "timeout", 2)
        _fail(emitter, source, "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1, f"no cluster emitted for source={source}"
        assert clusters[0].source == source
        assert clusters[0].payload["failure_type"] == "timeout"
