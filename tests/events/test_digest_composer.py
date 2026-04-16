"""Tests for events.subscribers.digest_composer — 3x/day structured digests."""

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
