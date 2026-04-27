"""Tests for events.subscribers.digest_composer — 3x/day structured digests."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.digest_composer import DigestComposer


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


class TestDigestComposer:
    def test_compose_from_events(self, bus):
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "VP Finance", "source": "Indeed"})
        bus.emit(EventType.JOB_DISCOVERED, "scout", {"title": "FP&A Dir", "source": "LinkedIn"})
        bus.emit(EventType.JOB_SCORED, "matcher", {"title": "VP Finance", "score": 8.5})
        bus.emit(EventType.CRON_COMPLETED, "jobflow-scout", {"duration": 120})
        bus.emit(EventType.CRON_COMPLETED, "jobflow-matcher", {"duration": 45})

        composer = DigestComposer(bus)
        digest = composer.compose()

        assert "HERMES DIGEST" in digest
        assert "scout" in digest.lower() or "Scout" in digest
        assert "2" in digest  # 2 jobs discovered

    def test_compose_empty_when_no_events(self, bus):
        composer = DigestComposer(bus)
        digest = composer.compose()
        assert "No activity" in digest or "HERMES DIGEST" in digest

    def test_compose_includes_action_items(self, bus):
        bus.emit(EventType.APPLICATION_READY, "applier", {"company": "Acme", "title": "VP Tax"})
        bus.emit(EventType.FOLLOWUP_DUE, "tracker", {"company": "Deloitte", "days": 14})

        composer = DigestComposer(bus)
        digest = composer.compose()

        assert "ACTION" in digest.upper()
        assert "Acme" in digest
        assert "Deloitte" in digest


class TestNotifierSnapshotHandshake:
    """Spec §7: jobflow-notifier writes digest-data.json, DigestComposer reads it."""

    def test_snapshot_file_missing_is_graceful(self, bus, tmp_path):
        """compose() works even when the notifier workspace has no snapshot."""
        composer = DigestComposer(
            bus,
            notifier_snapshot_path=tmp_path / "does-not-exist.json",
        )
        # Should not raise
        digest = composer.compose()
        assert "HERMES DIGEST" in digest

    def test_snapshot_malformed_is_graceful(self, bus, tmp_path):
        """Invalid JSON in the snapshot file doesn't break the digest."""
        snap = tmp_path / "digest-data.json"
        snap.write_text("not valid json {{{", encoding="utf-8")
        composer = DigestComposer(bus, notifier_snapshot_path=snap)
        digest = composer.compose()
        assert "HERMES DIGEST" in digest  # still produces a digest

    def test_snapshot_pipeline_counts_appear_in_digest(self, bus, tmp_path):
        """When the snapshot has pipeline counts, they appear in the digest body."""
        snap = tmp_path / "digest-data.json"
        snap.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_snapshot": {
                "total_active": 42,
                "by_stage": {
                    "discovered": 10,
                    "scored": 8,
                    "tailoring": 3,
                    "submitted": 15,
                    "interviewing": 5,
                    "rejected": 1,
                },
            },
        }), encoding="utf-8")

        composer = DigestComposer(bus, notifier_snapshot_path=snap)
        digest = composer.compose()

        # The total and at least one stage should land in the digest
        assert "42" in digest  # total_active
        assert "interviewing" in digest.lower() or "submitted" in digest.lower()

    def test_snapshot_stale_is_flagged(self, bus, tmp_path):
        """A snapshot older than 24 hours should be noted, not silently used."""
        snap = tmp_path / "digest-data.json"
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        snap.write_text(json.dumps({
            "generated_at": stale_ts,
            "pipeline_snapshot": {"total_active": 99},
        }), encoding="utf-8")

        composer = DigestComposer(bus, notifier_snapshot_path=snap)
        digest = composer.compose()

        # Should NOT surface the stale count as current truth
        assert "99" not in digest or "stale" in digest.lower()


def test_digest_composer_persists_last_digest_at(tmp_path, monkeypatch):
    from unittest.mock import patch
    from events.bus import EventBus
    from events.subscribers.digest_composer import DigestComposer
    from events.state import load_state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / "db.sqlite"
    bus = EventBus(db_path=db)
    try:
        with patch("events.subscribers.digest_composer.digest_state_path",
                   return_value=tmp_path / "digest_state.json"):
            d = DigestComposer(bus, send_telegram_fn=lambda m: None)
            d.compose()
            state = load_state(tmp_path / "digest_state.json", default={})
            assert "last_digest_at" in state
            assert state["last_digest_at"] is not None

            # New instance reads persisted state
            d2 = DigestComposer(bus, send_telegram_fn=lambda m: None)
            assert d2._last_digest_at == state["last_digest_at"]
    finally:
        bus.close()


class TestDeliveryTopicLookup:
    """Regression tests for v2 topic-key cutover (20260424T233627Z).

    DigestComposer._deliver_telegram, when no send_telegram_fn is injected
    (production gateway path — see events/gateway_integration.py), reads
    ~/.hermes/telegram/topics.json and resolves the target thread_id from
    a topic key. Pre-cutover the key was ``digests``; post-cutover it
    must be ``scribe_daily`` (the v2 destination for digest_generated /
    morning JobFlow digests, per TOPIC_ROUTING in telegram_notifier).

    With the dead key, the lookup returns ``{}``, ``thread_id`` becomes
    ``""``, and the ``if chat_id and thread_id:`` guard fails — the
    digest is silently dropped instead of being delivered.
    """

    def test_deliver_telegram_resolves_v2_scribe_daily_thread(
        self, bus, tmp_path,
    ):
        topics_path = tmp_path / "telegram" / "topics.json"
        topics_path.parent.mkdir(parents=True)
        topics_path.write_text(json.dumps({
            "group_chat_id": "-1001234567890",
            "topics": {
                # v2 keys (cutover 20260424T233627Z) — same fixture shape
                # as tests/events/test_telegram_notifier.py
                "watchdog_alerts": {"thread_id": 100},
                "jobflow_firehose": {"thread_id": 101},
                "jobflow_decisions": {"thread_id": 102},
                "scribe_daily": {"thread_id": 105},
                "curator_digest": {"thread_id": 107},
            },
        }))

        captured = {}
        def fake_deliver(job, body, skip_cron_framing=False):
            captured["target"] = job.get("deliver")
            captured["body"] = body

        composer = DigestComposer(bus)  # no send_telegram_fn → production branch
        with patch(
            "events.paths.telegram_topics_path",
            return_value=topics_path,
        ), patch(
            "cron.scheduler._deliver_result",
            side_effect=fake_deliver,
        ):
            composer._deliver_telegram("morning digest body")

        assert captured.get("target") == "telegram:-1001234567890:105", (
            f"expected target ending in :105 (scribe_daily), got "
            f"{captured.get('target')!r} — _deliver_telegram likely still "
            f"reading the dead v1 'digests' key"
        )
