"""Tests for events.subscribers.telegram_mirror — mirrors mailbox events to Agent Comms topic."""

import pytest

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.telegram_mirror import TelegramMirror


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "test.db")


class TestTelegramMirror:
    def test_only_processes_mailbox_events(self, bus):
        mirror = TelegramMirror(bus)
        assert mirror.event_types == [EventType.MAILBOX_MESSAGE]

    def test_formats_mirror_message(self, bus):
        mirror = TelegramMirror(bus)
        event = Event.create(
            EventType.MAILBOX_MESSAGE, "matcher",
            {"message_type": "SCORE_RESULT", "from": "matcher", "to": "main",
             "summary": "3 jobs scored"},
        )
        msg = mirror.format_mirror_message(event)
        assert "matcher" in msg
        assert "main" in msg
        assert "SCORE_RESULT" in msg
