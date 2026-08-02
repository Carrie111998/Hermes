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

    def test_cluster_payload_preserves_available_failure_details(
        self, bus, emitter,
    ):
        details = {
            "error_code": "PG_CONNECT_REFUSED",
            "phase": "postgres_sync",
            "deadline_seconds": 1800,
            "exception_type": "OperationalError",
        }
        for consecutive in range(1, 4):
            emitter.on_job_completed(
                job_id="job-postgres-sync",
                job_name="postgres-sync",
                success=False,
                duration=1.0,
                error="connection refused",
                consecutive_errors=consecutive,
                failure_details=details,
            )

        payload = bus.query(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
        )[0].payload
        assert payload["failure_type"] == "network"
        assert payload["count"] == 3
        assert payload["error_code"] == "PG_CONNECT_REFUSED"
        assert payload["phase"] == "postgres_sync"
        assert payload["deadline_seconds"] == 1800
        assert payload["exception_type"] == "OperationalError"

    def test_cluster_payload_sanitizes_latest_cause(self, bus, emitter):
        secret = "sk-testabcdefghijklmnop"
        for consecutive in range(1, 4):
            emitter.on_job_completed(
                job_id="job-postgres-sync",
                job_name="postgres-sync",
                success=False,
                duration=1.0,
                error=f"connection refused Authorization: Bearer {secret}",
                consecutive_errors=consecutive,
            )

        payload = bus.query(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
        )[0].payload
        assert "latest_cause" in payload
        assert "connection refused" in payload["latest_cause"]
        assert secret not in payload["latest_cause"]
        assert secret not in bus.query(event_type=EventType.CRON_FAILED)[0].payload["error"]
        assert secret not in bus.query(
            event_type=EventType.CRON_FAILED_CONSECUTIVE,
        )[0].payload["error"]

    def test_explicit_latest_cause_wins_over_generic_error(self, bus, emitter):
        for consecutive in range(1, 4):
            emitter.on_job_completed(
                job_id="job-postgres-sync",
                job_name="postgres-sync",
                success=False,
                duration=1.0,
                error="wrapper failed",
                consecutive_errors=consecutive,
                failure_details={"latest_cause": "connection refused"},
            )

        payload = bus.query(
            event_type=EventType.AGENT_FAILURE_CLUSTER,
        )[0].payload
        assert payload["latest_cause"] == "connection refused"


class TestEmitterCanonicalSource:
    """The cron emitter feeds the FailureClusterDetector with the canonical
    agent identity (not the raw cron job name) so that the parallel
    MailboxTranslator path -- which uses canonical short names -- shares
    the same per-source window state and emits dedupable events.

    Background: profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md
    Option A.  Without canonical mapping, 'jobflow-applier' (cron) and
    'applier' (mailbox) report into separate detector buckets and surface
    as two near-simultaneous AGENT_FAILURE_CLUSTER events for the same
    underlying failure -- doubling Telegram noise on #watchdog_alerts.
    """

    def test_jobflow_prefix_emits_canonical_source(self, bus, emitter):
        _fail(emitter, "jobflow-applier", "timeout", 1)
        _fail(emitter, "jobflow-applier", "timeout", 2)
        _fail(emitter, "jobflow-applier", "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "applier"
        assert clusters[0].payload["source"] == "applier"

    def test_sentinel_vip_collapses_to_sentinel(self, bus, emitter):
        _fail(emitter, "sentinel-vip-evening", "timeout", 1)
        _fail(emitter, "sentinel-vip-evening", "timeout", 2)
        _fail(emitter, "sentinel-vip-evening", "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "sentinel"

    def test_three_failures_across_sentinel_vip_variants_still_cluster(
        self, bus, emitter,
    ):
        """sentinel-vip-evening, sentinel-vip-midday, sentinel-vip-morning
        are three different cron jobs that all represent the same agent.
        After canonicalisation, three failures across the variants must
        still cluster into ONE event with source='sentinel'."""
        _fail(emitter, "sentinel-vip-evening", "timeout", 1)
        _fail(emitter, "sentinel-vip-midday", "timeout", 2)
        _fail(emitter, "sentinel-vip-morning", "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "sentinel"

    def test_unknown_cron_name_passes_through_verbatim(self, bus, emitter):
        """Unknown shapes ('Pipeline Drift Audit', ad-hoc names) must NOT
        get collapsed onto a canonical agent.  Verbatim is the safe
        default."""
        _fail(emitter, "Pipeline Drift Audit", "timeout", 1)
        _fail(emitter, "Pipeline Drift Audit", "timeout", 2)
        _fail(emitter, "Pipeline Drift Audit", "timeout", 3)
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) == 1
        assert clusters[0].source == "Pipeline Drift Audit"


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
