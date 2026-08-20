"""Tests for events.subscribers.whatsapp_escalator — WhatsApp escalation with quiet hours."""

import json
import logging
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.routing_policy import classify
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

    def test_urgent_tier_application_failed_and_consecutive_cron(self):
        assert classify_tier(self._ev(EventType.APPLICATION_FAILED)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.CRON_FAILED_CONSECUTIVE)) == EscalationTier.URGENT

    def test_application_blocked_is_immediate(self):
        # v3: application_blocked is ACT at CRITICAL default priority —
        # JobFlow is stalled on Diego's input, so it breaks quiet hours
        # (was URGENT pre-v3).
        assert classify_tier(self._ev(EventType.APPLICATION_BLOCKED)) == EscalationTier.IMMEDIATE

    def test_urgent_tier_only_for_gateway_down(self):
        assert classify_tier(self._ev(EventType.GATEWAY_HEALTH, {"status": "down"})) == EscalationTier.URGENT
        # Gateway up is not urgent (not escalated at all)
        assert classify_tier(self._ev(EventType.GATEWAY_HEALTH, {"status": "up"})) is None

    def test_important_tier_requires_high_score_threshold(self):
        # Score >= 9.0 is important
        assert classify_tier(self._ev(EventType.JOB_HIGH_SCORE, {"score": 9.2})) == EscalationTier.IMPORTANT
        # Below 9.0 is not escalated
        assert classify_tier(self._ev(EventType.JOB_HIGH_SCORE, {"score": 8.9})) is None

    def test_urgent_tier_application_ready_and_followup(self):
        # v3: both are ACT (operator decisions) — URGENT, not the retired
        # IMPORTANT classification. Queued during quiet hours, 7:01 flush.
        assert classify_tier(self._ev(EventType.APPLICATION_READY)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.FOLLOWUP_DUE)) == EscalationTier.URGENT

    def test_devflow_decisions_escalate_urgent(self):
        # 2026-07-11 operator request: DevFlow decision signals escalate to
        # WhatsApp (URGENT) alongside the devflow_decisions Telegram topic.
        assert classify_tier(self._ev(EventType.DEVFLOW_APPROVAL_REQUESTED)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.DEVFLOW_PR_REVIEW_REQUESTED)) == EscalationTier.URGENT
        assert classify_tier(self._ev(EventType.DEVFLOW_BUILD_FAILED)) == EscalationTier.URGENT

    def test_secret_detected_escalates_immediate(self):
        # Security critical: breaks quiet hours like CREDENTIAL_LOSS.
        assert classify_tier(self._ev(EventType.SECRET_DETECTED)) == EscalationTier.IMMEDIATE

    def test_devflow_firehose_events_do_not_escalate(self):
        # Firehose lifecycle (NOT decisions) stays bus/Telegram-only.
        assert classify_tier(self._ev(EventType.DEVFLOW_RUN_STARTED)) is None
        assert classify_tier(self._ev(EventType.DEVFLOW_BUILD_SUCCEEDED)) is None

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
        # v3: application_blocked became IMMEDIATE; approval_request is the
        # archetypal URGENT (queued during quiet hours) event now.
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPROVAL_REQUEST, "tracker", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=True):
            assert escalator.should_deliver_now(event) is False

    def test_all_events_deliver_during_active_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(EventType.APPROVAL_REQUEST, "tracker", {})

        with patch.object(escalator, '_is_quiet_hours', return_value=False):
            assert escalator.should_deliver_now(event) is True


class TestMessageFormat:
    def test_handle_uses_one_computed_route_for_tier_and_verdict(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        event = Event.create(
            EventType.INTERVIEW_SIGNAL,
            "tracker",
            {"company": "Acme"},
        )

        with patch.object(
            escalator, "_is_quiet_hours", return_value=False,
        ), patch.object(
            escalator, "_deliver", return_value=True,
        ), patch(
            "events.subscribers.whatsapp_escalator.policy_classify",
            wraps=classify,
        ) as classify_route, patch.object(
            escalator, "format_message", return_value="rendered",
        ) as render:
            escalator.handle(event)

        assert classify_route.call_count == 1
        route = render.call_args.kwargs["route"]
        assert route.verdict == classify(event).verdict
        assert route.wa_tier == "immediate"

    def test_uses_one_computed_route_for_verdict_and_tier(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        event = Event.create(
            EventType.AGENT_ITERATION,
            "postgres-sync",
            {"reason": "success", "counters": {"exit_code": 1}},
            priority=Priority.CRITICAL,
        )

        with patch(
            "events.subscribers.whatsapp_escalator.policy_classify",
            wraps=classify,
        ) as classify_route:
            with patch(
                "events.formatting.format_whatsapp_message",
                return_value="rendered",
            ) as render:
                msg = escalator.format_message(event)

        assert msg == "rendered\n\nDetails in Telegram"
        assert classify_route.call_count == 1
        expected_route = classify(event)
        assert render.call_args.kwargs["verdict"] == expected_route.verdict

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

    def test_blocked_question_options_render_as_a_list(
        self, bus, quiet_config, queue_path,
    ):
        """A Workday listbox answer must be VERBATIM one of the tenant's own
        labels, so the labels have to reach the phone whole and unambiguously
        delimited -- an inline comma run cannot say where a label ends, and it
        rides inside `question`, which MailboxWatcher._summarize caps at 200."""
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Capital One",
             "question": "Answer needed for How Did You Hear About Us?",
             "options": ["Internet", "Contacted by Recruiter", "Job Fair"]},
        )
        msg = escalator.format_message(event)
        assert "1. Internet" in msg
        assert "2. Contacted by Recruiter" in msg
        assert "3. Job Fair" in msg
        assert "EXACTLY" in msg

    def test_blocked_question_without_options_is_unchanged(
        self, bus, quiet_config, queue_path,
    ):
        """A free-text question must not grow an empty choice list."""
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "What is your visa status?"},
        )
        msg = escalator.format_message(event)
        assert "EXACTLY" not in msg
        # format_whatsapp_message prepends the header; the blocked sentence is
        # a line of its own and must not have grown a choice list.
        assert "Application blocked at Acme: What is your visa status?" in msg


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
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
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
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
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

    def test_has_queued_messages_reflects_queue_state(
        self, bus, quiet_config, queue_path
    ):
        """has_queued_messages() is the signal the gateway flush gate keys on to
        tell a drained flush from a stranded one. Missing/empty → False; items → True."""
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        # Missing file
        assert escalator.has_queued_messages() is False
        # Empty list
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text("[]", encoding="utf-8")
        assert escalator.has_queued_messages() is False
        # Non-empty
        queue_path.write_text(json.dumps([{"message": "x", "queued_at": "t"}]),
                              encoding="utf-8")
        assert escalator.has_queued_messages() is True

    def test_total_delivery_failure_strands_whole_queue(
        self, bus, quiet_config, queue_path
    ):
        """The 2026-07-10 shape: WhatsApp bridge down at 7am → the FIRST chunk
        fails → the ENTIRE queue is preserved and has_queued_messages() stays
        True, so the gateway gate knows the flush did NOT drain and must retry
        (rather than burning the day's single attempt)."""
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue = [
            {"message": f"event_{i}", "queued_at": "2026-07-10T03:00:00"}
            for i in range(105)
        ]
        queue_path.write_text(json.dumps(queue), encoding="utf-8")

        def fake_send(msg):
            raise RuntimeError("WhatsApp bridge down (creds.json 0 bytes)")

        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=fake_send,
        )

        count = escalator.flush_queue()

        assert count == 0, "Nothing delivered when the bridge is down"
        remaining = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(remaining) == 105, "Total failure must preserve the whole queue"
        assert escalator.has_queued_messages() is True

    def test_queue_size_capped_drops_oldest(
        self, bus, quiet_config, queue_path
    ):
        """_queue_message caps the queue at _MAX_QUEUE_SIZE (drop-oldest) so a
        multi-day WhatsApp outage can't grow quiet_queue.json unbounded.

        The queue is SEEDED to just under the cap in one write rather than
        driven there by cap+25 _queue_message calls: every call re-reads and
        re-rewrites the whole growing file, so filling it that way costs ~20MB
        of small synchronous IO and blew the 30s per-test timeout on its own
        (killing the tests/events run at ~98%). Seeding and then stepping
        _queue_message ACROSS the boundary exercises the same overflow path in
        four file round-trips."""
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        cap = escalator._MAX_QUEUE_SIZE

        # Seed one short of the cap: msg_0 .. msg_{cap-2}.
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        queue_path.write_text(json.dumps([
            {"message": f"msg_{i}", "queued_at": "2026-07-27T03:00:00"}
            for i in range(cap - 1)
        ]), encoding="utf-8")

        # The next message lands exactly ON the cap — nothing dropped yet.
        escalator._queue_message(f"msg_{cap - 1}")
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue) == cap, f"expected cap {cap}, got {len(queue)}"
        assert queue[0]["message"] == "msg_0", "at the cap nothing is dropped"

        # Three more overflow it, one at a time.
        for i in range(cap, cap + 3):
            escalator._queue_message(f"msg_{i}")

        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue) == cap, f"expected cap {cap}, got {len(queue)}"
        # Oldest 3 dropped; newest preserved.
        assert queue[0]["message"] == "msg_3"
        assert queue[-1]["message"] == f"msg_{cap + 2}"

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
    """A coalesced burst with actionable degradations is at least as urgent
    as a single transition (v3: via routing_policy, not a static dict)."""
    event = Event.create(
        EventType.WATCHDOG_BURST, "watchdog",
        {"count": 2, "trigger": "burst_threshold", "transitions": [
            {"probe": "Hermes API Server :8642", "tier": "critical",
             "before": "healthy", "after": "down"},
        ]},
    )
    assert classify_tier(event) == EscalationTier.URGENT


class TestCredentialLoss:
    """R70 alert-gap fix (2026-07-10): a credential/infra loss must break through
    quiet hours by name, unlike the URGENT-tier watchdog_burst it replaces."""

    def _ev(self, payload=None):
        return Event(
            event_id="cl",
            event_type=EventType.CREDENTIAL_LOSS,
            source="watchdog",
            timestamp="2026-07-10T02:48:58Z",
            priority=EventType.CREDENTIAL_LOSS.default_priority,
            payload=payload or {},
        )

    def test_credential_loss_is_immediate_tier(self):
        # The whole point: IMMEDIATE, not the URGENT that watchdog_burst gets.
        assert classify_tier(self._ev()) == EscalationTier.IMMEDIATE

    def test_credential_loss_priority_is_critical(self):
        assert EventType.CREDENTIAL_LOSS.default_priority == Priority.CRITICAL

    def test_credential_loss_breaks_through_quiet_hours(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = self._ev({"probe": "WhatsApp session creds present",
                          "before": "healthy", "after": "down",
                          "detail": "creds.json 0 bytes"})
        with patch.object(escalator, "_is_quiet_hours", return_value=True):
            assert escalator.should_deliver_now(event) is True

    def test_credential_loss_message_names_the_probe(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config, queue_path=queue_path)
        event = self._ev({"probe": "WhatsApp session creds present",
                          "before": "healthy", "after": "down",
                          "detail": "creds.json 0 bytes -- ENOSPC truncation?"})
        msg = escalator.format_message(event)
        assert "WhatsApp session creds present" in msg
        assert "DOWN" in msg
        assert "creds.json 0 bytes" in msg


class TestModelRateLimitedMessage:
    """2026-08-14: MODEL_RATE_LIMITED landed with routing + an icon but NO arm
    in format_message, so a real live-fire page rendered as the generic
    key:value dump ("model_rate_limited: provider: deepseek · model: ... ·
    outcome: chain_exhausted"). Same sibling-table drift class events/coverage.py
    documents. These pin the plain-English wording — and specifically that the
    two ACT outcomes stay worded APART, because their remedy differs."""

    @staticmethod
    def _ev(outcome, **over):
        payload = {"provider": "deepseek", "model": "deepseek-v4-pro",
                   "reason": "rate_limit", "detector": "runtime",
                   "outcome": outcome, "fallback_provider": "openai-codex",
                   "fallback_model": "gpt-5.6-sol", "resets_at": "",
                   "diverted_calls": 52, "episode_opened_at": "x"}
        payload.update(over)
        return Event.create(event_type=EventType.MODEL_RATE_LIMITED,
                            source="agent-loop", payload=payload)

    def _msg(self, bus, quiet_config, queue_path, outcome, **over):
        escalator = WhatsAppEscalator(bus, quiet_config_path=quiet_config,
                                      queue_path=queue_path)
        return escalator.format_message(self._ev(outcome, **over))

    def test_diverted_names_the_model_that_absorbed_the_traffic(self, bus, quiet_config, queue_path):
        msg = self._msg(bus, quiet_config, queue_path, "diverted")
        assert "deepseek-v4-pro" in msg
        assert "gpt-5.6-sol" in msg
        assert "52" in msg

    def test_chain_exhausted_says_runs_are_failing(self, bus, quiet_config, queue_path):
        msg = self._msg(bus, quiet_config, queue_path, "chain_exhausted")
        assert "runs are failing" in msg
        assert "every fallback is exhausted" in msg

    def test_no_fallback_names_the_different_remedy(self, bus, quiet_config, queue_path):
        """chain_exhausted = wait it out; no_fallback = go configure one.
        Collapsing these into one string loses the only actionable difference."""
        msg = self._msg(bus, quiet_config, queue_path, "no_fallback")
        assert "NO fallback configured" in msg
        assert "Add a fallback provider" in msg
        assert "every fallback is exhausted" not in msg

    def test_recovered_reads_as_closure(self, bus, quiet_config, queue_path):
        msg = self._msg(bus, quiet_config, queue_path, "recovered")
        assert "is back" in msg

    def test_reset_time_is_surfaced_when_known(self, bus, quiet_config, queue_path):
        msg = self._msg(bus, quiet_config, queue_path, "chain_exhausted",
                        resets_at="2026-08-15T00:30:00+00:00")
        assert "Resets 2026-08-15T00:30:00+00:00" in msg

    def test_never_falls_back_to_the_raw_payload_dump(self, bus, quiet_config, queue_path):
        """The regression this class exists for."""
        for outcome in ("diverted", "chain_exhausted", "no_fallback", "recovered"):
            msg = self._msg(bus, quiet_config, queue_path, outcome)
            assert "detector: runtime" not in msg, f"{outcome} fell through to the generic dump"
            assert "model_rate_limited:" not in msg


class TestDeliveryFailureRequeue:
    """2026-07-11 hardening: failed sends are requeued into the bounded
    quiet queue instead of being dropped (observed loss 2026-07-11 11:29,
    bridge 503 'Not connected to WhatsApp')."""

    @staticmethod
    def _failing_send(msg):
        raise RuntimeError("WhatsApp bridge error (503): Not connected")

    def test_immediate_failure_requeues(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=self._failing_send,
        )
        event = Event.create(
            EventType.INTERVIEW_SIGNAL, "tracker", {"company": "Google"},
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(event)
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue) == 1
        assert "Google" in queue[0]["message"]

    def test_throttle_flush_failure_requeues_and_clears_buffer(
        self, bus, quiet_config, queue_path,
    ):
        import time as _time
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=self._failing_send,
        )
        event = Event.create(
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(event)
            assert len(escalator._throttle_buffer) == 1
            escalator._throttle_start = _time.monotonic() - (
                escalator.THROTTLE_WINDOW_SECONDS + 1
            )
            escalator._maybe_flush_throttle()
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue) == 1
        assert "Acme" in queue[0]["message"]
        assert escalator._throttle_buffer == []


class TestThrottleAgeOutOnPoll:
    """2026-07-11 hardening: a lone buffered escalation flushes once its
    window ages out (via poll()), instead of waiting for the NEXT event."""

    def test_lone_buffered_event_flushes_via_poll(
        self, bus, quiet_config, queue_path,
    ):
        import time as _time
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )
        event = Event.create(
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(event)
            assert sent == []  # buffered, not sent
            escalator._throttle_start = _time.monotonic() - (
                escalator.THROTTLE_WINDOW_SECONDS + 1
            )
            escalator.poll()
        assert len(sent) == 1
        assert "Acme" in sent[0]

    def test_age_out_during_quiet_hours_moves_to_queue(
        self, bus, quiet_config, queue_path,
    ):
        import time as _time
        sent = []
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: sent.append(msg),
        )
        event = Event.create(
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(event)
        escalator._throttle_start = _time.monotonic() - (
            escalator.THROTTLE_WINDOW_SECONDS + 1
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=True):
            escalator._maybe_flush_throttle()
        assert sent == []
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert len(queue) == 1
        assert escalator._throttle_buffer == []


class TestThrottlePersistence:
    """2026-07-11 hardening: the throttle buffer survives a restart
    (persisted via whatsapp_throttle_path under HERMES_HOME)."""

    def test_buffer_restored_by_new_instance(self, bus, quiet_config, queue_path):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: None,
        )
        event = Event.create(
            EventType.APPROVAL_REQUEST, "tracker",
            {"job_title": "Visa Analyst", "job_company": "Acme", "score": 9},
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(event)
        assert len(escalator._throttle_buffer) == 1

        fresh = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: None,
        )
        assert fresh._throttle_buffer == escalator._throttle_buffer
        assert fresh._throttle_start is not None


class TestWatchdogRelevanceGating:
    """2026-07-11 operator feedback: WhatsApp was flooded with count-55
    WATCHDOG_BURSTs of optional-tier container flaps and pure recoveries.
    Only degradations of critical/important-tier probes earn a phone ping;
    everything else stays on Telegram/bus."""

    def _burst(self, transitions) -> Event:
        return Event.create(
            EventType.WATCHDOG_BURST, "watchdog",
            {"watchdog_type": "watchdog_burst", "count": len(transitions),
             "trigger": "burst_threshold", "transitions": transitions},
            priority=Priority.NORMAL,
        )

    def test_optional_only_burst_does_not_escalate(self):
        event = self._burst([
            {"probe": "Container: devflow-api", "tier": "optional",
             "before": "healthy", "after": "down"},
            {"probe": "Container: devflow-worker", "tier": "optional",
             "before": "healthy", "after": "unknown"},
        ])
        assert classify_tier(event) is None

    def test_recovery_only_burst_does_not_escalate(self):
        event = self._burst([
            {"probe": "Bridge: hermes->devflow lag", "tier": "important",
             "before": "down", "after": "healthy"},
            {"probe": "Postgres :5437", "tier": "critical",
             "before": "down", "after": "healthy"},
        ])
        assert classify_tier(event) is None

    def test_important_degradation_escalates_urgent(self):
        event = self._burst([
            {"probe": "Bridge: hermes->devflow lag", "tier": "important",
             "before": "healthy", "after": "down"},
            {"probe": "Container: devflow-api", "tier": "optional",
             "before": "healthy", "after": "down"},
        ])
        assert classify_tier(event) == EscalationTier.URGENT

    def test_missing_tier_fails_open(self):
        # Producer drift (no tier field) must degrade to noisy, not silent.
        event = self._burst([
            {"probe": "mystery-probe", "before": "healthy", "after": "down"},
        ])
        assert classify_tier(event) == EscalationTier.URGENT

    def test_empty_transitions_fails_open(self):
        event = self._burst([])
        assert classify_tier(event) == EscalationTier.URGENT

    def test_probe_transition_recovery_does_not_escalate(self):
        event = Event.create(
            EventType.WATCHDOG_PROBE_TRANSITION, "watchdog",
            {"probe": "Bridge: hermes->devflow lag", "tier": "important",
             "before": "down", "after": "healthy"},
        )
        assert classify_tier(event) is None

    def test_probe_transition_optional_does_not_escalate(self):
        event = Event.create(
            EventType.WATCHDOG_PROBE_TRANSITION, "watchdog",
            {"probe": "Container: devflow-api", "tier": "optional",
             "before": "healthy", "after": "down"},
        )
        assert classify_tier(event) is None

    def test_probe_transition_critical_degradation_escalates(self):
        event = Event.create(
            EventType.WATCHDOG_PROBE_TRANSITION, "watchdog",
            {"probe": "Postgres :5437", "tier": "critical",
             "before": "healthy", "after": "down"},
        )
        assert classify_tier(event) == EscalationTier.URGENT


class TestHumanReadableMessages:
    """2026-07-11 operator feedback: escalated WhatsApp messages arrived as
    raw payload JSON truncated at 200 chars. Every escalated type must render
    complete plain-English sentences — no braces, no watchdog_type, no
    HTTPConnectionPool spew."""

    def _escalator(self, bus, quiet_config, queue_path):
        return WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )

    def test_watchdog_burst_message_is_readable(self, bus, quiet_config, queue_path):
        escalator = self._escalator(bus, quiet_config, queue_path)
        event = Event.create(
            EventType.WATCHDOG_BURST, "watchdog",
            {"watchdog_type": "watchdog_burst", "count": 2,
             "trigger": "burst_threshold",
             "transitions": [
                 {"probe": "Bridge: hermes->devflow lag", "tier": "important",
                  "category": "hermes", "before": "healthy", "after": "down"},
                 {"probe": "Container: devflow-api", "tier": "optional",
                  "category": "infra", "before": "healthy", "after": "down"},
             ]},
            priority=Priority.NORMAL,
        )
        msg = escalator.format_message(event)
        assert "Bridge: hermes->devflow lag" in msg
        assert "SYSTEM HEALTH ALERT" in msg          # plain header title
        assert "WATCHDOG_BURST" not in msg           # no enum jargon
        assert "{" not in msg and "}" not in msg     # no raw JSON
        assert "watchdog_type" not in msg
        assert "Details in Telegram" in msg

    def test_container_crash_loop_message_is_readable(self, bus, quiet_config,
                                                      queue_path):
        """The 2026-08-12 hindsight-app payload must page as English, not JSON.

        The body has one job beyond readability: warn that the tray row reads
        GREEN. A container that loops in bursts is healthy at most samples, so
        an alert that omits this reads as stale on arrival.
        """
        escalator = self._escalator(bus, quiet_config, queue_path)
        event = Event.create(
            EventType.CONTAINER_CRASH_LOOP, "watchdog",
            {"watchdog_type": "container_crash_loop",
             "container": "hindsight-app", "restarts_24h": 266,
             "restart_count_now": 266, "threshold": 20,
             "tray_state": "healthy", "tray_tier": "important",
             "tray_detail": "running, RestartCount stable (266), up 902s"},
            priority=Priority.HIGH,
        )
        msg = escalator.format_message(event)
        assert "hindsight-app" in msg
        assert "266" in msg
        assert "CONTAINER CRASH-LOOPING" in msg       # plain header title
        assert "CONTAINER_CRASH_LOOP" not in msg      # no enum jargon
        assert "{" not in msg and "}" not in msg      # no raw JSON
        assert "watchdog_type" not in msg
        assert "HEALTHY" in msg, "must say the row currently reads green"

    def test_silence_alert_message_is_readable(self, bus, quiet_config, queue_path):
        escalator = self._escalator(bus, quiet_config, queue_path)
        event = Event.create(
            EventType.WATCHDOG_SILENCE_ALERT, "watchdog",
            {"watchdog_type": "watchdog_silence_alert",
             "source": "devflow-bridge",
             "expected_cadence_seconds": 300,
             "time_since_last_seconds": 509,
             "last_seen": "2026-07-11T17:55:10.218930+00:00",
             "severity": "silent"},
            priority=Priority.HIGH,
        )
        msg = escalator.format_message(event)
        assert "devflow-bridge went quiet" in msg
        assert "8m 29s" in msg
        assert "{" not in msg and "watchdog_type" not in msg

    def test_gateway_health_message_humanizes_conn_error(self, bus, quiet_config, queue_path):
        escalator = self._escalator(bus, quiet_config, queue_path)
        detail = (
            "HTTPConnectionPool(host='127.0.0.1', port=3000): "
            "Max retries exceeded with url: /health (Caused by "
            "NewConnectionError('HTTPConnection(host=127.0.0.1, "
            "port=3000): Failed to establish a new connection'))"
        )
        event = Event.create(
            EventType.GATEWAY_HEALTH, "system",
            {"platform": "whatsapp", "status": "down", "detail": detail},
            priority=Priority.HIGH,
        )
        msg = escalator.format_message(event)
        assert "whatsapp gateway is DOWN" in msg
        assert "connection refused" in msg
        assert "127.0.0.1:3000" in msg
        assert "HTTPConnectionPool" not in msg

    def test_failure_cluster_message_is_readable(self, bus, quiet_config, queue_path):
        escalator = self._escalator(bus, quiet_config, queue_path)
        event = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, "watchdog",
            {"watchdog_type": "agent_failure_cluster",
             "source": "mailbox:tailor", "cluster_size": 4,
             "last_event_type": "cron_failed",
             "last_timestamp": "2026-07-11T18:03:00+00:00",
             "fingerprint": "mailbox:tailor|abc123"},
            priority=Priority.HIGH,
        )
        msg = escalator.format_message(event)
        assert "mailbox:tailor" in msg
        assert "4 times in a row" in msg
        assert "{" not in msg

    def test_fallback_never_emits_raw_json(self, bus, quiet_config, queue_path):
        # A type with no dedicated branch must render scalar pairs, not
        # a truncated json.dumps of the payload.
        escalator = self._escalator(bus, quiet_config, queue_path)
        event = Event.create(
            EventType.CRON_STALE, "cron",
            {"job_name": "nightly-consolidate", "minutes_stale": 95,
             "nested": {"deep": "dict"}},
        )
        msg = escalator.format_message(event)
        assert "job_name: nightly-consolidate" in msg
        assert "minutes_stale: 95" in msg
        assert "{" not in msg and "'deep'" not in msg


class TestResourcePressurePaging:
    """disk_critical routes ACT -> the phone. Until 2026-08-14 this type had no
    branch here, so it fell to the scalar fallback, which takes scalars[:6] in
    payload order and stops BEFORE disk_c_free_gb — a disk-full page that never
    mentions the disk. Never caught because disk_critical had fired 0 times."""

    def _event(self, free_gb=2.4, band="imminent", edge=3, change="band_change"):
        return Event.create(
            EventType.RESOURCE_PRESSURE, "system",
            {
                "reasons": ["disk_low", "disk_critical"],
                "commit_used_gb": 83.32, "commit_limit_gb": 127.2,
                "commit_pct": 65.5, "phys_used_pct": 75.8,
                "phys_available_gb": 15.3, "pagefile_allocated_gb": 64.0,
                "pagefile_growth_gb_10min": 0.0,
                "disk_c_free_gb": free_gb, "disk_band": band,
                "disk_band_edge_gb": edge, "change": change,
                "thresholds": {"disk_free_gb": 45.0},
            },
        )

    def test_the_page_names_the_disk_and_its_severity(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        msg = escalator.format_message(self._event())
        assert "IMMINENT" in msg.upper()
        assert "2.4" in msg
        assert "disk_c_free_gb" not in msg   # not the raw scalar fallback

    def test_a_sustained_repeat_never_reaches_the_phone(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        # URGENT is throttle-buffered rather than delivered synchronously, so
        # the buffer — not _deliver — is what "reached the phone lane" means.
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(self._event(change="sustained_repeat"))
        assert escalator._throttle_buffer == []

    def test_a_band_change_does_reach_the_phone(
        self, bus, quiet_config, queue_path,
    ):
        escalator = WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
        )
        with patch.object(escalator, "_is_quiet_hours", return_value=False):
            escalator.handle(self._event(change="band_change"))
        assert len(escalator._throttle_buffer) == 1
        assert "IMMINENT" in escalator._throttle_buffer[0].upper()
class TestRenderedMessageObservability:
    """The formatter needs a witness. A delivery receipt proves DELIVERY,
    never that the delivered TEXT is right -- which is how the UNKNOWN
    AGENT_NOTE header rendered wrong on every Telegram delivery while 759
    tests and three clean receipts passed. _deliver logs the header line so
    the formatter's output exists somewhere on disk.

    The narrowing is the load-bearing half: the body is CALLER CONTENT and
    must never reach a log file, so the negative assertion below is the
    point of this class. Reading the header back only proves the header is
    logged; it proves nothing about what else leaked alongside it.
    """

    _MARKER = "zq-body-marker-must-not-be-logged-7431"

    def _escalator(self, bus, quiet_config, queue_path):
        return WhatsAppEscalator(
            bus, quiet_config_path=quiet_config, queue_path=queue_path,
            send_fn=lambda msg: None,
        )

    def test_header_is_logged_and_body_is_not(
        self, bus, quiet_config, queue_path, caplog,
    ):
        escalator = self._escalator(bus, quiet_config, queue_path)
        message = (
            "🚨 URGENT - Acme - 12:00 UTC\n\n"
            + self._MARKER + "\nmore body"
        )

        with caplog.at_level(
            logging.INFO, logger="events.subscribers.whatsapp_escalator",
        ):
            assert escalator._deliver(message) is True

        blob = "\n".join(r.getMessage() for r in caplog.records)
        assert "WhatsAppEscalator sending:" in blob
        assert "URGENT - Acme - 12:00 UTC" in blob
        # The negative: no part of the body reached any log record.
        assert self._MARKER not in blob
        assert "more body" not in blob
        # A length, never content -- enough to catch truncation.
        header = message.splitlines()[0]
        assert "(+" + str(len(message) - len(header)) + " body chars)" in blob

    def test_empty_message_does_not_raise(
        self, bus, quiet_config, queue_path, caplog,
    ):
        escalator = self._escalator(bus, quiet_config, queue_path)
        with caplog.at_level(
            logging.INFO, logger="events.subscribers.whatsapp_escalator",
        ):
            assert escalator._deliver("") is True
        assert "WhatsAppEscalator sending:" in "\n".join(
            r.getMessage() for r in caplog.records
        )
