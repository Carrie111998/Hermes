"""Tests for events.subscribers.telegram_notifier — Telegram forum topic routing."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.telegram_notifier import TelegramNotifier, TOPIC_ROUTING


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def topics_config(tmp_path):
    config = {
        "group_chat_id": "-1001234567890",
        "topics": {
            "alerts": {"thread_id": 100, "name": "Alerts & Actions"},
            "scout": {"thread_id": 101, "name": "Scout / Discoveries"},
            "matcher": {"thread_id": 102, "name": "Matcher / Scores"},
            "tailor_applier": {"thread_id": 103, "name": "Tailor & Applier"},
            "tracker": {"thread_id": 104, "name": "Tracker / Pipeline"},
            "digests": {"thread_id": 105, "name": "Digests & Summaries"},
            "system": {"thread_id": 106, "name": "System Health"},
            "agent_comms": {"thread_id": 107, "name": "Agent Comms"},
        },
    }
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    config = {
        "scout": {"mode": "all"},
        "matcher": {"mode": "all"},
        "system": {"mode": "digest_only"},
        "agent_comms": {"mode": "significant_only"},
    }
    path = tmp_path / "telegram" / "verbosity.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config))
    return path


class TestTopicRouting:
    def test_all_event_types_have_routing(self):
        for et in EventType:
            assert et.type_string in TOPIC_ROUTING, \
                f"EventType {et.type_string} missing from TOPIC_ROUTING"

    def test_scout_events_route_to_scout(self):
        assert TOPIC_ROUTING["job_discovered"] == "scout"
        assert TOPIC_ROUTING["job_vip_discovered"] == "scout"

    def test_critical_events_route_to_alerts(self):
        assert TOPIC_ROUTING["application_blocked"] == "alerts"
        assert TOPIC_ROUTING["interview_signal"] == "alerts"
        assert TOPIC_ROUTING["offer_signal"] == "alerts"

    def test_topic_routing_covers_all_domain_events(self):
        from events.subscribers.telegram_notifier import TOPIC_ROUTING
        from events.schema import EventType
        required = {
            EventType.JOB_DISCOVERED, EventType.JOB_VIP_DISCOVERED,
            EventType.JOB_SCORED, EventType.JOB_HIGH_SCORE,
            EventType.TAILOR_COMPLETED, EventType.APPLICATION_READY,
            EventType.APPLICATION_SUBMITTED, EventType.APPLICATION_FAILED,
            EventType.APPLICATION_BLOCKED, EventType.INTERVIEW_SIGNAL,
            EventType.OFFER_SIGNAL, EventType.STAGE_TRANSITION,
            EventType.FOLLOWUP_DUE, EventType.AGENT_ERROR,
            EventType.CRON_FAILED_CONSECUTIVE, EventType.GATEWAY_HEALTH,
        }
        # TOPIC_ROUTING is a flat {event_string: topic_string} mapping;
        # an event is "covered" if its type_string is a key.
        covered = {et for et in EventType if et.type_string in TOPIC_ROUTING}
        missing = required - covered
        assert not missing, f"TOPIC_ROUTING missing: {missing}"


class TestTelegramNotifier:
    def test_formats_message(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "VP Finance", "company": "Acme", "source": "Indeed"},
        )
        msg = notifier.format_message(event)
        assert "job_discovered" in msg.lower() or "JOB_DISCOVERED" in msg
        assert "scout" in msg.lower()

    def test_resolves_topic_for_event(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(EventType.JOB_DISCOVERED, "scout", {})
        target = notifier.resolve_target(event)
        assert target == ("telegram", "-1001234567890", "101")

    def test_cross_posts_critical_to_alerts(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_FAILED, "applier", {"error": "timeout"},
            priority=Priority.CRITICAL,
        )
        targets = notifier.resolve_all_targets(event)
        topic_ids = [t[2] for t in targets]
        # application_failed routes to alerts directly
        assert "100" in topic_ids  # alerts

    def test_loads_topics_config(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        assert notifier.group_chat_id == "-1001234567890"
        assert notifier.topics["scout"]["thread_id"] == 101


class TestLowPriorityBatching:
    """Tests for low-priority event batching and flush behavior.

    Uses event types that route to topics with 'all' verbosity mode
    (scout, matcher) to avoid verbosity filtering interference.
    """

    def test_low_priority_event_is_buffered(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        # job_discovered routes to "scout" topic which has mode="all"
        event = Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        )
        notifier.handle(event)

        # Low-priority should be buffered, not delivered yet
        assert len(sent) == 0
        assert len(notifier._batch_buffer) > 0

    def test_normal_priority_event_delivered_immediately(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        # job_scored routes to "matcher" topic which has mode="all"
        event = Event.create(
            EventType.JOB_SCORED, "matcher",
            {"score": 7.5, "title": "Engineer", "company": "Beta"},
            priority=Priority.NORMAL,
        )
        notifier.handle(event)

        assert len(sent) == 1

    def test_high_priority_event_delivered_immediately(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "Visa status?"},
            priority=Priority.CRITICAL,
        )
        notifier.handle(event)

        assert len(sent) >= 1

    def test_flush_delivers_batched_messages(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        # Buffer two low-priority events on the "scout" topic (mode=all)
        for i in range(2):
            event = Event.create(
                EventType.JOB_DISCOVERED, "scout",
                {"title": f"Job {i}", "company": "Acme", "source": "Indeed"},
                priority=Priority.LOW,
            )
            notifier.handle(event)

        assert len(sent) == 0  # still buffered

        # Force flush with max_age=0 (flush all)
        notifier._flush_stale_batches(max_age=0)

        assert len(sent) == 1
        assert "Batched (2 events)" in sent[0]

    def test_shutdown_flushes_all_batches(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        # Use scout topic (mode=all) with low priority to ensure buffering
        event = Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        )
        notifier.handle(event)

        assert len(sent) == 0

        notifier.shutdown()

        assert len(sent) == 1
