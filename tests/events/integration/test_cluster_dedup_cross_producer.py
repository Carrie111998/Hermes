"""Cross-producer cluster dedup integration test.

The watchdog-dedup proposal (2026-04-29) calls out that ``CronEventEmitter``
(cron exit codes) and ``MailboxTranslator`` (structured ERROR mailbox
messages) BOTH emit ``AGENT_FAILURE_CLUSTER`` for the same underlying
agent failure, with subtly different ``source`` strings.  Option A's fix
canonicalises the source at the ``FailureClusterDetector.record()``
boundary, which makes the per-source window file-backed state shared
across the two producers when they read/write the same state path
(``events.paths.failure_cluster_state_path``).

This test stitches the two producers together against ONE state path and
verifies that:

  * Three failures spread across the cron path AND the mailbox path
    accumulate into ONE cluster window (canonical key 'applier'), not
    two windows that each fail to reach threshold.
  * The single emitted cluster event carries the canonical source string
    so any downstream LRU dedup at the receiver works.

If this test ever regresses, expect the Telegram ``#watchdog_alerts``
topic to start receiving ~2x cluster events for the same incident again.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.producers.cron_emitter import CronEventEmitter
from events.schema import EventType
from events.subscribers.mailbox_translator import MailboxTranslator


@pytest.fixture
def shared_state_setup(tmp_path):
    """Both producers read/write the same failure-cluster state file."""
    state_path = tmp_path / "events" / "failure_cluster_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return state_path


def _record_mailbox_error(translator: MailboxTranslator, source: str, message: str):
    """Drive the mailbox translator's cluster path directly so the test
    does not depend on bus.subscribe() (which has a first-registration
    cursor-default-to-head behaviour that skips events emitted before
    the subscriber's first poll -- broken on main as of 2026-04-29).
    """
    translator._record_error_for_clustering(
        outer_payload={"from": source, "to": "main"},
        inner={"message": message, "source_agent": source},
        correlation_id=None,
    )


def test_cron_and_mailbox_paths_dedupe_on_canonical_source(
    tmp_path, shared_state_setup,
):
    """Three failures: 2 from cron path ('jobflow-applier') and 1 from
    mailbox path ('applier').  All three must collapse into ONE cluster
    event with source='applier'.

    Without canonicalisation, the cron path would key under
    'jobflow-applier' and the mailbox path under 'applier' -- two
    separate windows of size 2 + 1, neither crossing the 3-threshold.

    With canonicalisation at record(), all three rows write under
    'applier' and the third record() call returns ClusterInfo.
    """
    state_path = shared_state_setup
    bus = EventBus(db_path=tmp_path / "event_bus.db")
    try:
        with patch(
            "events.producers.cron_emitter.failure_cluster_state_path",
            lambda: state_path,
        ), patch(
            "events.subscribers.mailbox_translator.failure_cluster_state_path",
            lambda: state_path,
        ):
            emitter = CronEventEmitter(bus)
            translator = MailboxTranslator(bus)

            # Mix: cron, mailbox, cron -- same canonical agent, same failure type.
            emitter.on_job_completed(
                job_id="j1", job_name="jobflow-applier",
                success=False, duration=1.0, error="captcha bail",
                consecutive_errors=1,
            )
            _record_mailbox_error(translator, "applier", "captcha")
            emitter.on_job_completed(
                job_id="j3", job_name="jobflow-applier",
                success=False, duration=1.0, error="captcha",
                consecutive_errors=2,
            )

            clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
            assert len(clusters) == 1, (
                f"Expected 1 cluster across cron+mailbox paths; got {len(clusters)}. "
                f"Without canonicalisation, the two paths key into separate windows "
                f"and never share state."
            )
            assert clusters[0].source == "applier"
            assert clusters[0].payload["source"] == "applier"
            assert clusters[0].payload["failure_type"] == "captcha"
            assert clusters[0].payload["count"] == 3
    finally:
        bus.close()


def test_cron_path_alone_emits_canonical_cluster(
    tmp_path, shared_state_setup,
):
    """Sanity: the cron path on its own still emits a cluster, with the
    canonical source.  Guards against the cron-path canonicalisation
    being accidentally removed."""
    state_path = shared_state_setup
    bus = EventBus(db_path=tmp_path / "event_bus.db")
    try:
        with patch(
            "events.producers.cron_emitter.failure_cluster_state_path",
            lambda: state_path,
        ):
            emitter = CronEventEmitter(bus)
            for i in range(1, 4):
                emitter.on_job_completed(
                    job_id=f"j{i}", job_name="jobflow-applier",
                    success=False, duration=1.0, error="captcha",
                    consecutive_errors=i,
                )
            clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
            assert len(clusters) == 1
            assert clusters[0].source == "applier"
    finally:
        bus.close()


def test_mailbox_path_alone_emits_canonical_cluster(
    tmp_path, shared_state_setup,
):
    """Sanity: the mailbox path on its own still emits a cluster, with
    the canonical source.  Guards against the mailbox-path
    canonicalisation being accidentally removed."""
    state_path = shared_state_setup
    bus = EventBus(db_path=tmp_path / "event_bus.db")
    try:
        with patch(
            "events.subscribers.mailbox_translator.failure_cluster_state_path",
            lambda: state_path,
        ):
            translator = MailboxTranslator(bus)
            for _ in range(3):
                _record_mailbox_error(translator, "jobflow-applier", "captcha")
            clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
            assert len(clusters) == 1
            assert clusters[0].source == "applier"
    finally:
        bus.close()
