"""Tests for events.subscribers.memory_writer -- routes events to memory layers."""

from unittest.mock import MagicMock, patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.memory_writer import MemoryWriter, MEMORY_ROUTING


class TestMemoryRouting:
    def test_high_score_routes_to_gbrain(self):
        assert "gbrain" in MEMORY_ROUTING[EventType.JOB_HIGH_SCORE]["targets"]

    def test_interview_routes_to_both(self):
        targets = MEMORY_ROUTING[EventType.INTERVIEW_SIGNAL]["targets"]
        assert "gbrain" in targets
        assert "mempalace" in targets

    def test_cron_failed_consecutive_routes_to_memory_md(self):
        assert "memory_md" in MEMORY_ROUTING[EventType.CRON_FAILED_CONSECUTIVE]["targets"]

    def test_cron_completed_not_in_routing(self):
        assert EventType.CRON_COMPLETED not in MEMORY_ROUTING


class TestMemoryWriter:
    def test_skips_non_routed_events(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(EventType.CRON_COMPLETED, "scout", {})
        # Should not raise
        writer.handle(event)

    def test_builds_gbrain_content(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
        )
        content = writer._build_content(event, "gbrain")
        assert "Acme" in content
        assert "VP Finance" in content
