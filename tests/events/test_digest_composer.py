"""Tests for events.subscribers.digest_composer — 3x/day structured digests."""

import json
from pathlib import Path

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
            "generated_at": "2026-04-16T11:55:00Z",
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
        # generated 2 days ago
        snap.write_text(json.dumps({
            "generated_at": "2026-04-14T12:00:00Z",
            "pipeline_snapshot": {"total_active": 99},
        }), encoding="utf-8")

        composer = DigestComposer(bus, notifier_snapshot_path=snap)
        digest = composer.compose()

        # Should NOT surface the stale count as current truth
        assert "99" not in digest or "stale" in digest.lower()
