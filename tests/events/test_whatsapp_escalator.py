"""Tests for events.subscribers.whatsapp_escalator — WhatsApp escalation with quiet hours."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.whatsapp_escalator import WhatsAppEscalator


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def quiet_config(tmp_path):
    config = {
        "enabled": True,
        "start": "23:00",
        "end": "07:00",
        "timezone": "America/New_York",
        "breakthrough_events": ["interview_signal", "offer_signal"],
    }
    path = tmp_path / "notifications" / "quiet_hours.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def queue_path(tmp_path):
    return tmp_path / "notifications" / "quiet_queue.json"


class TestEscalationCriteria:
    def test_interview_signal_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Google"})
        assert escalator.should_escalate(event) is True

    def test_cron_completed_does_not_escalate(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.CRON_COMPLETED, "scout", {})
        assert escalator.should_escalate(event) is False

    def test_job_high_score_above_9_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.JOB_HIGH_SCORE, "matcher", {"score": 9.1})
        assert escalator.should_escalate(event) is True

    def test_job_high_score_below_9_does_not_escalate(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.JOB_HIGH_SCORE, "matcher", {"score": 8.8})
        assert escalator.should_escalate(event) is False

    def test_application_blocked_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})
        assert escalator.should_escalate(event) is True

    def test_cron_failed_consecutive_escalates(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.CRON_FAILED_CONSECUTIVE, "system", {})
        assert escalator.should_escalate(event) is True


class TestQuietHours:
    def test_breakthrough_during_quiet_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Acme"})

        with patch.object(escalator, '_is_quiet_hours', return_value=True):
            assert escalator.should_deliver_now(event) is True  # breakthrough

    def test_non_breakthrough_queued_during_quiet_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=True):
            assert escalator.should_deliver_now(event) is False

    def test_all_events_deliver_during_active_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPLICATION_BLOCKED, "applier", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            assert escalator.should_deliver_now(event) is True


class TestMessageFormat:
    def test_plain_text_no_markdown(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "What is your visa status?"},
        )
        msg = escalator.format_message(event)
        assert "**" not in msg  # no markdown bold
        assert "Acme" in msg
        assert "Details in Telegram" in msg
