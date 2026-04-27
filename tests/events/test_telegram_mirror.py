"""Tests for events.subscribers.telegram_mirror — mirrors mailbox events to Agent Comms topic."""

import json
from unittest.mock import patch

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


class TestDeliveryTopicLookup:
    """Regression test for v2 topic-key cutover (20260424T233627Z).

    TelegramMirror._deliver_to_agent_comms, when no send_fn is injected
    (production gateway path — see events/gateway_integration.py), reads
    topics.json and resolves the target thread_id from a topic key. Pre-
    cutover the key was ``agent_comms``; post-cutover the v1 distinction
    between ``digests`` (notifications) and ``agent_comms`` (agent-to-agent
    chatter) collapsed into a single ``scribe_daily`` topic — the v2
    destination for ``mailbox_message`` per telegram_notifier.TOPIC_ROUTING.

    With the dead key, the lookup returns ``{}``, ``thread_id`` becomes
    ``""``, the early-return guard fires, and the mirror silently drops
    every mailbox-message mirror.
    """

    def test_deliver_to_agent_comms_resolves_v2_scribe_daily_thread(
        self, bus, tmp_path,
    ):
        topics_path = tmp_path / "telegram" / "topics.json"
        topics_path.parent.mkdir(parents=True)
        topics_path.write_text(json.dumps({
            "group_chat_id": "-1001234567890",
            "topics": {
                "watchdog_alerts": {"thread_id": 100},
                "jobflow_firehose": {"thread_id": 101},
                "scribe_daily": {"thread_id": 105},
            },
        }))

        captured = {}
        def fake_deliver(job, body, skip_cron_framing=False):
            captured["target"] = job.get("deliver")
            captured["body"] = body

        mirror = TelegramMirror(bus, topics_path=topics_path)  # no send_fn
        with patch("cron.scheduler._deliver_result", side_effect=fake_deliver):
            mirror._deliver_to_agent_comms("matcher → main: SCORE_RESULT")

        assert captured.get("target") == "telegram:-1001234567890:105", (
            f"expected target ending in :105 (scribe_daily), got "
            f"{captured.get('target')!r} — _deliver_to_agent_comms likely "
            f"still reading the dead v1 'agent_comms' key"
        )
