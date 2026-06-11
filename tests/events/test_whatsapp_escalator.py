"""Tests for events.subscribers.whatsapp_escalator — WhatsApp escalation with quiet hours."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.schema import Event, EventType, Priority
from events.subscribers.whatsapp_escalator import (
    WhatsAppEscalator,
    EscalationTier,
    classify_tier,
)


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


class TestEscalationTier:
    """Spec Section 2.2: 4 tiers (Immediate / Urgent / Important / Digest)."""

    def _ev(self, event_type: EventType, payload=None) -> Event:
        return Event(
            event_id="t",
            event_type=event_type,
            source="test",
            timestamp="2026-04-16T00:00:00Z",
            priority=event_type.default_priority,
            payload=payload or {},
        )

    def test_immediate_tier_breaks_through_quiet_hours(self):
        assert classify_tier(self._ev(EventType.INTERVIEW_SIGNAL)) == EscalationTier.IMMEDIATE
        assert classify_tier(self._ev(EventType.OFFER_SIGNAL)) == EscalationTier.IMMEDIATE

    def test_urgent_tier_application_blocked_and_failed(self):
        assert classify_tier(self._ev(EventType.APPLICATION_BLOCKED)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.APPLICATION_FAILED)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.CRON_FAILED_CONSECUTIVE)) == EscalationTier.URGENT

    def test_urgent_tier_only_for_gateway_down(self):
        assert classify_tier(self._ev(EventType.GATEWAY_HEALTH, {"status": "down"})) == EscalationTier.URGENT
        # Gateway up is not urgent (not escalated at all)
        assert classify_tier(self._ev(EventType.GATEWAY_HEALTH, {"status": "up"})) is None

    def test_important_tier_requires_high_score_threshold(self):
        # Score >= 9.0 is important
        assert classify_tier(self._ev(EventType.JOB_HIGH_SCORE, {"score": 9.2})) == EscalationTier.IMPORTANT
        # Below 9.0 is not escalated
        assert classify_tier(self._ev(EventType.JOB_HIGH_SCORE, {"score": 8.9})) is None

    def test_important_tier_application_ready_and_followup(self):
        assert classify_tier(self._ev(EventType.APPLICATION_READY)) == EscalationTier.IMPORTANT
        assert classify_tier(self._ev(EventType.FOLLOWUP_DUE)) == EscalationTier.IMPORTANT

    def test_non_escalated_events_return_none(self):
        assert classify_tier(self._ev(EventType.CRON_COMPLETED)) is None
        assert classify_tier(self._ev(EventType.JOB_DISCOVERED)) is None
        assert classify_tier(self._ev(EventType.MAILBOX_MESSAGE)) is None

    def test_tier_ordering(self):
        """Tiers are orderable — Immediate > Urgent > Important > Digest."""
        assert EscalationTier.IMMEDIATE.priority > EscalationTier.URGENT.priority
        assert EscalationTier.URGENT.priority > EscalationTier.IMPORTANT.priority
        assert EscalationTier.IMPORTANT.priority > EscalationTier.DIGEST.priority


class TestQuietHoursConfigHardening:
    """Invalid quiet_hours.json must fail safe, not silently let WA through at 3am."""

    def test_malformed_json_falls_back_to_defaults(self, bus, tmp_path):
        """A corrupt config file shouldn't crash the subscriber on startup."""
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text("{not valid json", encoding="utf-8")

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path)
        # Defaults: 23:00-07:00 ET, enabled
        assert esc._quiet_config.get("enabled") is True
        assert esc._quiet_config.get("start") == "23:00"
        assert esc._quiet_config.get("end") == "07:00"

    def test_invalid_timezone_does_not_cause_silent_delivery(self, bus, tmp_path):
        """Bad timezone string should log and default to conservative quiet behavior."""
        config = {
            "enabled": True,
            "start": "23:00",
            "end": "07:00",
            "timezone": "Mars/Olympus_Mons",  # invalid
            "breakthrough_events": [],
        }
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text(json.dumps(config))

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path)
        # With bad tz, we must NOT silently return False (which would let
        # WhatsApp through 24/7).  Conservative: treat as quiet hours.
        assert esc._is_quiet_hours() is True

    def test_invalid_time_format_falls_back_conservatively(self, bus, tmp_path):
        """Non-HH:MM start/end strings shouldn't cause silent delivery."""
        config = {
            "enabled": True,
            "start": "eleven pm",   # garbage
            "end": "07:00",
            "timezone": "America/New_York",
        }
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text(json.dumps(config))

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path)
        # Same as bad tz: conservative, treat as quiet
        assert esc._is_quiet_hours() is True

    def test_disabled_config_bypasses_quiet_hours(self, bus, tmp_path):
        """Explicitly disabled quiet hours should always return False."""
        config = {"enabled": False, "start": "23:00", "end": "07:00",
                  "timezone": "America/New_York"}
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text(json.dumps(config))

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path)
        assert esc._is_quiet_hours() is False


class TestQueueFileConfig:
    """Spec Section 5: queue_file in quiet_hours.json is the configured queue path."""

    def test_queue_file_field_is_honored(self, bus, tmp_path):
        """queue_file in quiet_hours.json overrides the default location."""
        custom_queue = tmp_path / "custom" / "my_queue.json"
        config = {
            "enabled": True,
            "start": "23:00",
            "end": "07:00",
            "timezone": "America/New_York",
            "breakthrough_events": [],
            "queue_file": str(custom_queue),
        }
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text(json.dumps(config))

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path)
        assert esc._queue_path == custom_queue

    def test_explicit_queue_path_wins_over_config(self, bus, tmp_path):
        """Constructor arg takes precedence over quiet_hours.json queue_file."""
        config = {
            "queue_file": str(tmp_path / "from_config.json"),
            "enabled": True,
            "start": "23:00", "end": "07:00",
            "timezone": "America/New_York",
        }
        config_path = tmp_path / "quiet_hours.json"
        config_path.write_text(json.dumps(config))
        explicit = tmp_path / "explicit.json"

        esc = WhatsAppEscalator(bus, quiet_config_path=config_path, queue_path=explicit)
        assert esc._queue_path == explicit


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


class TestThrottleBuffer:
    """Tests for the 15-minute throttle window on non-breakthrough events."""

    def test_breakthrough_events_bypass_throttle(self, bus, quiet_config, queue_path):
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )

        event = Event.create(
            EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Google"},
        )

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            escalator.handle(event)

        # Breakthrough should deliver immediately, not buffer
        assert len(sent) == 1
        assert "Google" in sent[0]
        assert len(escalator._throttle_buffer) == 0

    def test_non_breakthrough_events_are_buffered(self, bus, quiet_config, queue_path):
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )

        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "Visa?"},
        )

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            escalator.handle(event)

        # Non-breakthrough should be added to throttle buffer, not sent yet
        assert len(sent) == 0
        assert len(escalator._throttle_buffer) == 1

    def test_shutdown_flushes_throttle_buffer(self, bus, quiet_config, queue_path):
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )

        # Buffer a non-breakthrough event
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "Visa?"},
        )

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            escalator.handle(event)

        assert len(sent) == 0

        escalator.shutdown()

        assert len(sent) == 1
        assert "Acme" in sent[0]


class TestFlushQueueChunking:
    """flush_queue must chunk oversized queues to fit under the WhatsApp
    bridge's express.json() ~100KB body limit and WhatsApp's 4096-char
    per-message text limit. Regression: previously concatenated all queued
    messages into a single payload, hitting HTTP 413 when the queue grew
    over a few days of accumulation (e.g. 1814 watchdog probe events =
    508KB on 2026-04-30)."""

    def test_oversized_queue_chunks_under_whatsapp_message_limit(
        self, bus, quiet_config, queue_path
    ):
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        # 200 messages × ~250 chars each = 50KB total. The pre-fix code would
        # concatenate this into one ~50KB payload that exceeds WhatsApp's
        # 4096-char text limit.
        queue = [
            {"message": f"event_{i}: " + "x" * 240, "queued_at": "2026-04-30T01:00:00"}
            for i in range(200)
        ]
        queue_path.write_text(json.dumps(queue), encoding="utf-8")

        sent = []

        def fake_send(msg):
            # Simulate WhatsApp's hard 4096-char limit; refuse anything larger.
            if len(msg) > 4096:
                raise RuntimeError(f"WhatsApp message too long: {len(msg)} > 4096")
            sent.append(msg)

        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=fake_send,
        )

        count = escalator.flush_queue()

        assert count == 200, f"Expected all 200 messages flushed, got {count}"
        assert len(sent) > 1, "Oversized queue must produce multiple chunks"
        for i, msg in enumerate(sent):
            assert len(msg) <= 4096, f"Chunk {i} is {len(msg)} chars (>4096)"
        # Queue file drained on full success.
        assert json.loads(queue_path.read_text(encoding="utf-8")) == []

    def test_partial_chunk_failure_preserves_remainder(
        self, bus, quiet_config, queue_path
    ):
        """If a mid-flight chunk fails, deliver the successful chunks and
        preserve the unsent remainder for retry (rather than losing
        already-delivered chunks or replaying duplicates)."""
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = [
            {"message": f"event_{i}: " + "x" * 240, "queued_at": "2026-04-30T01:00:00"}
            for i in range(60)
        ]
        queue_path.write_text(json.dumps(queue), encoding="utf-8")

        sent = []

        def fake_send(msg):
            if len(msg) > 4096:
                raise RuntimeError(f"oversized: {len(msg)}")
            # Fail on the 2nd chunk attempt — before appending, so `sent`
            # only reflects truly delivered chunks.
            if len(sent) == 1:
                raise RuntimeError("simulated bridge error on 2nd chunk")
            sent.append(msg)

        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=fake_send,
        )

        count = escalator.flush_queue()

        assert len(sent) == 1, "Only first chunk should have succeeded"
        assert 0 < count < 60, f"Partial drain expected, got count={count}"

        remaining = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(remaining) == 60 - count, (
            f"Remainder mismatch: delivered={count}, remaining={len(remaining)}"
        )
        assert len(remaining) > 0, "Failed chunks must be preserved for retry"

    def test_small_queue_still_drains_in_single_chunk(
        self, bus, quiet_config, queue_path
    ):
        """Existing behavior: a small queue fits in one chunk and drains
        with a single delivery. Chunking is purely additive for oversized
        queues."""
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = [
            {"message": f"event_{i}", "queued_at": "2026-04-30T01:00:00"}
            for i in range(5)
        ]
        queue_path.write_text(json.dumps(queue), encoding="utf-8")

        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )

        count = escalator.flush_queue()

        assert count == 5
        assert len(sent) == 1
        assert json.loads(queue_path.read_text(encoding="utf-8")) == []


class TestNotificationDeliveredReverseSignal:
    """NOTIFICATION_DELIVERED + NOTIFICATION_FAILED reverse signal for
    WhatsApp side. Mirrors the telegram-notifier contract per the
    2026-04-30 design doc.

    WhatsApp's classify_tier returns None for the new types (they're
    not in _TIER_BY_EVENT), so should_escalate naturally drops them
    today. The explicit cycle guard is defense-in-depth: if anyone
    ever adds a tier mapping for delivery events, the guard prevents
    a self-consume loop.
    """

    def test_emits_notification_delivered_on_success(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: None,  # success
        )
        # interview_signal is IMMEDIATE tier -> bypasses throttle
        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]

        escalator.handle(original)

        delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
        assert len(delivered) == 1, (
            f"expected exactly one NOTIFICATION_DELIVERED, got {len(delivered)}"
        )
        evt = delivered[0]
        assert evt.priority == Priority.LOW
        assert evt.payload["original_event_id"] == original_id
        assert evt.payload["original_event_type"] == "interview_signal"
        assert evt.payload["platform"] == "whatsapp"
        assert evt.payload["latency_ms"] >= 0
        assert evt.correlation_id == original_id

    def test_emits_notification_failed_on_exception(
        self, bus, quiet_config, queue_path,
    ):
        def boom(msg):
            raise RuntimeError("WhatsApp bridge connection refused")

        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=boom,
        )
        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]

        # _deliver() catches the exception; it returns False but never
        # propagates. The reverse signal must fire.
        escalator.handle(original)

        failed = bus.query(event_type=EventType.NOTIFICATION_FAILED)
        assert len(failed) == 1, (
            f"expected exactly one NOTIFICATION_FAILED, got {len(failed)}"
        )
        evt = failed[0]
        assert evt.priority == Priority.NORMAL
        assert evt.payload["original_event_id"] == original_id
        assert evt.payload["platform"] == "whatsapp"
        assert evt.payload["error"]["kind"] == "RuntimeError"
        assert "connection refused" in evt.payload["error"]["message"]

    def test_emit_failure_does_not_break_delivery(
        self, bus, quiet_config, queue_path, monkeypatch,
    ):
        """If bus.emit raises while emitting the reverse signal, the
        actual WhatsApp send still succeeded and no exception bubbles
        out of handle(). Production failure mode: a transient SQLite
        lock on event_bus.db must not silence legit interview alerts.
        """
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )
        bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen"},
            priority=Priority.CRITICAL,
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]

        emit_calls = {"count": 0}

        def maybe_raising_emit(*args, **kwargs):
            emit_calls["count"] += 1
            raise RuntimeError("event_bus locked")

        monkeypatch.setattr(bus, "emit", maybe_raising_emit)

        # Must not raise.
        escalator.handle(original)

        assert len(sent) == 1, (
            "delivery must complete despite reverse-signal emit failure"
        )
        assert emit_calls["count"] >= 1, (
            "_deliver() must have attempted the reverse-signal emit"
        )

    def test_does_not_consume_own_notification_delivered_events(
        self, bus, quiet_config, queue_path,
    ):
        """Cycle guard: NOTIFICATION_DELIVERED feeding back into handle()
        must short-circuit. Today it's naturally dropped at should_escalate
        because classify_tier returns None — the explicit guard is
        defense-in-depth against future tier-mapping changes.
        """
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )
        delivered_event = Event.create(
            EventType.NOTIFICATION_DELIVERED, "whatsapp-escalator",
            {
                "original_event_id": "abc-123",
                "platform": "whatsapp",
                "target": {"phone_or_channel": "WHATSAPP_HOME_CHANNEL"},
                "latency_ms": 250,
            },
            priority=Priority.LOW,
        )
        escalator.handle(delivered_event)

        assert sent == [], "subscriber must not deliver its own delivery events"
        # Throttle buffer must also stay empty — the guard short-circuits
        # before ANY downstream work, not just before _deliver().
        assert escalator._throttle_buffer == []
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []

    def test_does_not_consume_own_notification_failed_events(
        self, bus, quiet_config, queue_path,
    ):
        """Cycle guard for NOTIFICATION_FAILED at NORMAL priority. Critical
        because a future tier mapping for delivery failures (e.g. URGENT
        if Diego wants paged failures) WOULD start consuming them, and
        without the guard this loops indefinitely.
        """
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )
        failed_event = Event.create(
            EventType.NOTIFICATION_FAILED, "whatsapp-escalator",
            {
                "original_event_id": "abc-123",
                "platform": "whatsapp",
                "target": {"phone_or_channel": "WHATSAPP_HOME_CHANNEL"},
                "latency_ms": 50,
                "error": {"kind": "RuntimeError", "message": "timeout"},
            },
            priority=Priority.NORMAL,
        )
        escalator.handle(failed_event)

        assert sent == [], "subscriber must not retry its own failure events"
        assert escalator._throttle_buffer == []
        assert bus.query(event_type=EventType.NOTIFICATION_FAILED) == []

    def test_throttled_delivery_does_not_emit_per_event(
        self, bus, tmp_path, queue_path,
    ):
        """IMPORTANT (URGENT/IMPORTANT tier) events that aren't IMMEDIATE
        breakthrough hit the 15-min throttle buffer rather than _deliver()
        directly. Phase 1 scope: throttled deliveries do NOT emit
        per-event reverse signals (mirrors Telegram's batched scoping).
        Failures still emit when the eventual flush attempts a send.
        """
        # Quiet hours OFF: this test exercises only the throttle tier.
        # With the shared 23:00-07:00 fixture and the real wall clock, any
        # run inside the window (e.g. the 02:30 nightly gate) would route
        # the event to the quiet queue and never reach the throttle buffer.
        quiet_off = tmp_path / "notifications" / "quiet_hours_off.json"
        quiet_off.parent.mkdir(parents=True, exist_ok=True)
        quiet_off.write_text(json.dumps({"enabled": False}))
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_off, queue_path=queue_path,
            send_fn=lambda msg: None,
        )
        # application_ready is IMPORTANT tier (throttled, not immediate)
        bus.emit(
            event_type=EventType.APPLICATION_READY, source="applier",
            payload={"company": "Acme", "title": "VP Finance"},
            priority=Priority.HIGH,
        )
        original = bus.query(event_type=EventType.APPLICATION_READY)[0]

        escalator.handle(original)

        # Throttled into buffer, not delivered yet
        assert len(escalator._throttle_buffer) == 1
        # No reverse signal until the throttle window closes
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []


def test_watchdog_burst_maps_to_urgent_tier():
    """A coalesced burst is at least as urgent as a single transition."""
    from events.subscribers.whatsapp_escalator import _TIER_BY_EVENT, EscalationTier
    from events.schema import EventType

    assert _TIER_BY_EVENT[EventType.WATCHDOG_BURST] == EscalationTier.URGENT
