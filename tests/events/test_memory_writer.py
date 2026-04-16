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


class TestMemPalaceWrite:
    """Tests for the MemPalace integration in MemoryWriter._write_mempalace."""

    def test_interview_signal_writes_to_mempalace(self, tmp_path):
        """INTERVIEW_SIGNAL should call mempalace add_drawer with the event content."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.INTERVIEW_SIGNAL, "tracker",
            {"company": "Acme", "detail": "Phone screen Tuesday 2pm"},
        )

        captured_call = {}

        def fake_add_drawer(collection, wing, room, content, source_file, chunk_index, agent):
            captured_call.update(
                collection=collection,
                wing=wing, room=room, content=content,
                source_file=source_file, chunk_index=chunk_index, agent=agent,
            )
            return True

        def fake_get_collection(*args, **kwargs):
            return "mock-collection"

        with patch("mempalace.miner.add_drawer", fake_add_drawer), \
             patch("mempalace.palace.get_collection", fake_get_collection):
            writer.handle(event)

        assert captured_call, "mempalace.add_drawer should have been called"
        # Event content should flow through to the drawer
        assert "Acme" in captured_call["content"]
        assert "Phone screen" in captured_call["content"]
        # source_file should identify the event uniquely
        assert event.event_id in captured_call["source_file"]
        # Wing/room should be hermes-specific (not a user wing)
        assert captured_call["wing"].startswith("hermes")
        # Room should correspond to the event type
        assert captured_call["room"] == "interview_signal"
        assert captured_call["agent"] == "hermes-event-bus"

    def test_mempalace_missing_is_graceful(self, tmp_path):
        """If mempalace is not installed, MemoryWriter should not crash."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event = Event.create(
            EventType.OFFER_SIGNAL, "tracker",
            {"company": "Acme", "detail": "Offer letter attached"},
        )

        # Simulate mempalace not being importable
        import builtins
        real_import = builtins.__import__

        def stub_import(name, *args, **kwargs):
            if name.startswith("mempalace"):
                raise ImportError("no mempalace")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", stub_import):
            # Should not raise
            writer.handle(event)

    def test_collection_is_cached_across_writes(self, tmp_path):
        """Subsequent writes should reuse the same collection object."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        calls = {"count": 0}

        def fake_get_collection(*args, **kwargs):
            calls["count"] += 1
            return "shared-collection"

        def noop_add_drawer(*args, **kwargs):
            return True

        with patch("mempalace.miner.add_drawer", noop_add_drawer), \
             patch("mempalace.palace.get_collection", fake_get_collection):
            writer._write_mempalace(
                Event.create(EventType.INTERVIEW_SIGNAL, "t", {"company": "A"}),
                "content-1",
            )
            writer._write_mempalace(
                Event.create(EventType.OFFER_SIGNAL, "t", {"company": "B"}),
                "content-2",
            )

        assert calls["count"] == 1, "get_collection should only be called once (cached)"


class TestCorrelationIdDedup:
    """Tests for correlation_id deduplication in MemoryWriter."""

    def test_same_correlation_id_processed_once(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event1 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-abc-123",
        )
        event2 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-abc-123",
        )

        # First call should process
        writer.handle(event1)
        assert "corr-abc-123" in writer._seen_correlation_ids

        # Second call with same correlation_id should be skipped
        # We verify by checking rate counters: only one write should have been attempted
        gbrain_count_before = len(writer._rate_counters.get("gbrain", []))

        writer.handle(event2)

        gbrain_count_after = len(writer._rate_counters.get("gbrain", []))
        assert gbrain_count_after == gbrain_count_before  # no new write

    def test_different_correlation_ids_both_processed(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        writer = MemoryWriter(bus)

        event1 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Acme", "title": "VP Finance", "platform": "Workday"},
            correlation_id="corr-111",
        )
        event2 = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"company": "Beta Inc", "title": "Engineer", "platform": "Lever"},
            correlation_id="corr-222",
        )

        writer.handle(event1)
        writer.handle(event2)

        # Both correlation_ids should be recorded as seen
        assert "corr-111" in writer._seen_correlation_ids
        assert "corr-222" in writer._seen_correlation_ids

        # Both should have triggered writes (2 gbrain entries in rate counters)
        assert len(writer._rate_counters.get("gbrain", [])) == 2
