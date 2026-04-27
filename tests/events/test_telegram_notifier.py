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
    # v2 topic keys (Hermes Telegram cutover 20260424T233627Z) — match the
    # post-cutover production ~/.hermes/telegram/topics.json. Thread IDs are
    # chosen so existing test assertions (101 = jobflow_firehose primary,
    # 105 = scribe_daily for mailbox/digest, 100 = watchdog_alerts for
    # application_failed) continue to hold without churn.
    config = {
        "group_chat_id": "-1001234567890",
        "topics": {
            "watchdog_alerts": {"thread_id": 100, "name": "Watchdog Alerts"},
            "jobflow_firehose": {"thread_id": 101, "name": "JobFlow Firehose"},
            "jobflow_decisions": {"thread_id": 102, "name": "JobFlow Decisions"},
            "devflow_firehose": {"thread_id": 103, "name": "DevFlow Firehose"},
            "devflow_decisions": {"thread_id": 104, "name": "DevFlow Decisions"},
            "scribe_daily": {"thread_id": 105, "name": "Scribe Daily"},
            "security_and_system": {"thread_id": 106, "name": "Security & System"},
            "curator_digest": {"thread_id": 107, "name": "Curator Digest"},
            "critic_proposals": {"thread_id": 108, "name": "Critic Proposals"},
            "hermes_milestones": {"thread_id": 109, "name": "Hermes Milestones"},
        },
    }
    path = tmp_path / "telegram" / "topics.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config))
    return path


@pytest.fixture
def verbosity_config(tmp_path):
    config = {
        "jobflow_firehose": {"mode": "all"},
        "jobflow_decisions": {"mode": "all"},
        "watchdog_alerts": {"mode": "all"},
        "security_and_system": {"mode": "digest_only"},
        "curator_digest": {"mode": "significant_only"},
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
        # v2 cutover 20260424T233627Z: scout-domain firehose absorbed into
        # jobflow_firehose (formerly the standalone "scout" topic).
        assert TOPIC_ROUTING["job_discovered"] == "jobflow_firehose"
        assert TOPIC_ROUTING["job_vip_discovered"] == "jobflow_firehose"

    def test_critical_events_route_to_alerts(self):
        # v2 cutover split the v1 "alerts" topic into two:
        #   - watchdog_alerts: system/applier failures
        #   - jobflow_decisions: human-action signals (interviews, offers)
        assert TOPIC_ROUTING["application_blocked"] == "watchdog_alerts"
        assert TOPIC_ROUTING["interview_signal"] == "jobflow_decisions"
        assert TOPIC_ROUTING["offer_signal"] == "jobflow_decisions"

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

    def test_agent_failure_cluster_routes_to_watchdog_alerts(self):
        """agent_failure_cluster fires from the watchdog detector and is
        an operational alert (cluster of failures across agents). It routes
        to watchdog_alerts.

        Regression: 2026-04-26 — TOPIC_ROUTING contained two entries for
        'agent_failure_cluster' (one mapping to watchdog_alerts, one to
        critic_proposals). Python dict literals are last-write-wins, so the
        cluster events silently went only to critic_proposals; watchdog_alerts
        never received them.

        Why watchdog_alerts is the right primary topic: the event source is
        the watchdog detector and the existing watchdog flood gate in
        TelegramNotifier.handle() lists agent_failure_cluster alongside the
        other watchdog signals. The Critic also consumes the cluster (Phase
        3.1, agent-failure-cluster branch) but produces critic_proposal
        events as its output — and those already route to critic_proposals.
        Trigger and proposal are separate events with separate topics.
        """
        assert TOPIC_ROUTING["agent_failure_cluster"] == "watchdog_alerts"


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

    def test_notification_mailbox_message_routes_to_digests(
        self, bus, topics_config, verbosity_config,
    ):
        """NOTIFICATION mailbox messages (morning digest, user-facing content)
        must route to the ``digests`` topic — NOT to ``agent_comms`` (the
        default mailbox_message topic) where ``significant_only`` drops the
        default LOW priority.

        Regression: 2026-04-19 — the Sunday morning digest was emitted to the
        bus but silently dropped at the filter before reaching the user.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.MAILBOX_MESSAGE, "notifier",
            {
                "message_type": "NOTIFICATION",
                "from": "notifier",
                "to": "main",
                "summary": "🌅 JobFlow Morning Digest — Sun Apr 19",
            },
        )
        target = notifier.resolve_target(event)
        assert target == ("telegram", "-1001234567890", "105")  # digests thread

    def test_non_notification_mailbox_message_falls_through_to_default(
        self, bus, topics_config, verbosity_config,
    ):
        """Agent-to-agent mailbox messages (SCORE_RESULT, TAILOR_REQUEST, etc.)
        fall through to the default mailbox routing — TOPIC_ROUTING
        ``mailbox_message`` → ``scribe_daily`` post v2 cutover. Only
        NOTIFICATION message_type triggers the explicit override branch in
        resolve_target(); the override target is also ``scribe_daily`` in
        v2 (the v1 ``digests`` vs ``agent_comms`` distinction collapsed at
        the cutover, 20260424T233627Z), so both paths produce the same
        thread_id. This test guards against the override branch firing
        for non-NOTIFICATION messages or against TOPIC_ROUTING regressing
        on the default.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.MAILBOX_MESSAGE, "matcher",
            {
                "message_type": "SCORE_RESULT",
                "from": "matcher",
                "to": "main",
                "summary": "score 9.0 for Acme",
            },
        )
        target = notifier.resolve_target(event)
        assert target == ("telegram", "-1001234567890", "105")  # scribe_daily thread

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
        assert notifier.topics["jobflow_firehose"]["thread_id"] == 101

    def test_cron_completed_long_summary_is_trimmed_for_mission_control(self, bus, topics_config, verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        summary = "Learning-loop maintenance pass complete.\n\n" + "\n".join(
            f"- detail line {i}" for i in range(1, 80)
        )
        event = Event.create(
            EventType.CRON_COMPLETED,
            "learning-loop",
            {"duration": 705.2, "output_summary": summary},
            priority=Priority.HIGH,
        )

        msg = notifier.format_message(event)

        assert "Duration: 705.2s" in msg
        assert "Mission Control trimmed the rest" in msg
        assert "- detail line 79" not in msg


class TestSecretDetectedFormatting:
    """SR-408 regression (2026-04-19) — SECRET_DETECTED must render as a
    compact, human-readable Telegram message, not a generic key:value dump
    of the full payload.

    Root cause of the 2026-04-19 flood's cryptic appearance:
    `_format_payload` had no branch for SECRET_DETECTED, so the generic
    fallback (`f"{k}: {v}" for k, v in p.items()`) emitted six lines
    including `match_preview: ****************************` walls (the
    `matched_string` of a LevelDB binary chunk, up to 2000+ chars of
    asterisks) and noise like `finding_hash: sha256:…` /
    `gitleaks_version: v8.30.1` that operators cannot act on.

    Payload contract comes from scanner.py::emit_event() — keys:
        rule_id, file_path, line_no, match_preview, finding_hash,
        gitleaks_version.
    """

    def test_secret_detected_body_shows_rule_path_line_preview(
        self, bus, topics_config, verbosity_config,
    ):
        """Body must contain the four operator-relevant fields."""
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.SECRET_DETECTED, "sr-001-secret-scanner",
            {
                "rule_id": "aws-access-token",
                "file_path": "C:/Users/diego/.env",
                "line_no": 5,
                "match_preview": "AKIA****XYZ1234",
                "finding_hash": "sha256:abc123",
                "gitleaks_version": "v8.30.1",
            },
        )
        body = notifier._format_payload(event)
        assert "aws-access-token" in body, "rule_id must appear"
        assert "C:/Users/diego/.env" in body, "file_path must appear"
        assert "5" in body, "line_no must appear"
        assert "AKIA" in body, "masked preview must appear"

    def test_secret_detected_body_omits_internal_fields(
        self, bus, topics_config, verbosity_config,
    ):
        """finding_hash and gitleaks_version are internal — must not leak
        into the user-facing Telegram body. finding_hash shows up as
        ``sha256:…`` and adds no actionable info (dedup identity only).
        gitleaks_version is audit metadata; it belongs in the event, not
        the message.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.SECRET_DETECTED, "sr-001-secret-scanner",
            {
                "rule_id": "aws-access-token",
                "file_path": "C:/Users/diego/.env",
                "line_no": 5,
                "match_preview": "AKIA****",
                "finding_hash": "sha256:abc123def456",
                "gitleaks_version": "v8.30.1",
            },
        )
        body = notifier._format_payload(event)
        assert "sha256:" not in body, "finding_hash must be suppressed"
        assert "gitleaks_version" not in body, "gitleaks_version label must be suppressed"
        assert "v8.30.1" not in body, "gitleaks version value must be suppressed"
        assert "finding_hash" not in body, "finding_hash label must be suppressed"

    def test_secret_detected_body_is_compact(
        self, bus, topics_config, verbosity_config,
    ):
        """Body must be at most 3 short lines. A generic fallback that
        enumerates 6 payload fields produced the 'asterisk wall' the user
        called out as 'very cryptic and weird messages I have no clue
        what the fuck they mean' on 2026-04-19.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.SECRET_DETECTED, "sr-001-secret-scanner",
            {
                "rule_id": "aws-access-token",
                "file_path": "C:/Users/diego/.env",
                "line_no": 5,
                "match_preview": "AKIA****XYZ1234",
                "finding_hash": "sha256:abc123",
                "gitleaks_version": "v8.30.1",
            },
        )
        body = notifier._format_payload(event)
        lines = body.splitlines()
        assert 0 < len(lines) <= 3, (
            f"SECRET_DETECTED body must be ≤3 lines; got {len(lines)}: {body!r}"
        )

    def test_secret_detected_tolerates_missing_fields(
        self, bus, topics_config, verbosity_config,
    ):
        """Scanner payload should always be complete, but missing fields
        must not raise — fallback to '?' placeholders. A KeyError here
        would bubble up through the subscriber loop and stall the cursor.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.SECRET_DETECTED, "sr-001-secret-scanner",
            {},  # empty payload
        )
        body = notifier._format_payload(event)
        # Should not raise; should produce a well-formed (if placeholder-heavy) body.
        assert body, "empty payload must still yield a non-empty body"


class TestLowPriorityBatching:
    """Tests for low-priority event batching and flush behavior.

    Uses event types that route to topics with 'all' verbosity mode
    (jobflow_firehose, jobflow_decisions in v2) to avoid verbosity
    filtering interference.
    """

    def test_low_priority_event_is_buffered(self, bus, topics_config, verbosity_config):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

        # job_discovered routes to "jobflow_firehose" topic (v2) with mode="all"
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

        # job_scored routes to "jobflow_firehose" topic (v2) with mode="all"
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

        # Buffer two low-priority events on the "jobflow_firehose" topic (mode=all)
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

        # Use jobflow_firehose topic (v2, mode=all) with low priority to ensure buffering
        event = Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        )
        notifier.handle(event)

        assert len(sent) == 0

        notifier.shutdown()

        assert len(sent) == 1


def test_notifier_restores_batch_buffer_on_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telegram").mkdir()
    (tmp_path / "telegram" / "topics.json").write_text(
        '{"group_chat_id": "-1", "topics": {"system": {"thread_id": 15}}}')
    (tmp_path / "telegram" / "verbosity.json").write_text(
        '{"system": {"mode": "all"}}')
    from events.bus import EventBus
    from events.subscribers.telegram_notifier import TelegramNotifier
    bus = EventBus(db_path=tmp_path / "db.sqlite")
    try:
        n1 = TelegramNotifier(bus, send_fn=lambda *a, **k: None)
        n1._batch_buffer["-1:15"] = ["pending msg 1", "pending msg 2"]
        n1._persist_batch_buffer()

        n2 = TelegramNotifier(bus, send_fn=lambda *a, **k: None)
        assert n2._batch_buffer.get("-1:15") == ["pending msg 1", "pending msg 2"]
    finally:
        bus.close()
