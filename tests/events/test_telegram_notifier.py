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


class TestAgentIterationRouting:
    """AGENT_ITERATION uses per-agent topic dispatch (AGENT_TOPIC_MAP)
    via resolve_target() rather than the static TOPIC_ROUTING table.
    These tests pin that each agent name lands in the right topic.
    """

    def _make_event(self, agent_name: str):
        from events.schema import Event, EventType
        return Event.create(
            EventType.AGENT_ITERATION, agent_name,
            {"agent": agent_name, "summary": "test summary"},
        )

    def test_jobflow_agent_routes_to_jobflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        for agent in ["scout", "matcher", "tailor", "applier", "tracker", "sentinel"]:
            target = notifier.resolve_target(self._make_event(agent))
            assert target[2] == "101", f"{agent} expected jobflow_firehose(101), got {target[2]}"

    def test_critic_routes_to_critic_proposals(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("critic"))
        assert target[2] == "108"

    def test_curator_routes_to_curator_digest(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("curator"))
        assert target[2] == "107"

    def test_watchdog_routes_to_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("watchdog"))
        assert target[2] == "100"

    def test_devflow_routes_to_devflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        for agent in ["devflow", "devflow-standup", "devflow-bridge"]:
            target = notifier.resolve_target(self._make_event(agent))
            assert target[2] == "103", f"{agent} expected devflow_firehose(103), got {target[2]}"

    def test_unknown_agent_falls_back_to_jobflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("some-future-agent"))
        assert target[2] == "101"

    def test_empty_agent_payload_falls_back_to_default(
        self, bus, topics_config, verbosity_config,
    ):
        from events.schema import Event, EventType
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        # Missing agent field → default fallback
        event = Event.create(
            EventType.AGENT_ITERATION, "unknown",
            {"summary": "no agent name"},
        )
        target = notifier.resolve_target(event)
        assert target[2] == "101"

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

    def test_cross_posts_high_priority_event_to_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        """CROSS_POST_TO_ALERTS events at HIGH+ priority must hit BOTH the
        primary topic AND watchdog_alerts.

        Regression: 2026-04-27 — the v2 cutover (20260424T233627Z) renamed
        the catch-all ``alerts`` topic to ``watchdog_alerts``, but
        resolve_all_targets() kept reading ``self.topics.get("alerts", {})``.
        Post-cutover topics.json has no ``alerts`` key, so the lookup
        returned ``{}``, ``alerts_thread`` became ``""``, the guard
        ``if alerts_thread and alerts_thread != primary_thread`` failed,
        and CROSS_POST_TO_ALERTS events (application_ready, followup_due,
        interview_signal, job_high_score, offer_signal) at HIGH+ silently
        went ONLY to their primary firehose topic — exactly the noisy
        stream a busy operator needs them surfaced OUT of.

        JOB_HIGH_SCORE is a clean witness: its primary topic is
        ``jobflow_decisions`` (thread 102), so a working cross-post must
        ADD watchdog_alerts (thread 100). With the bug, only 102 appears.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.JOB_HIGH_SCORE, "matcher",
            {"job_id": "abc-123", "score": 9.4, "title": "VP Finance"},
            priority=Priority.HIGH,
        )
        targets = notifier.resolve_all_targets(event)
        topic_ids = [t[2] for t in targets]
        assert "102" in topic_ids, (
            f"primary jobflow_decisions thread missing from {topic_ids}"
        )
        assert "100" in topic_ids, (
            f"watchdog_alerts cross-post missing from {topic_ids} "
            f"— resolve_all_targets() likely still reading the dead 'alerts' "
            f"key instead of 'watchdog_alerts'"
        )

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


class TestAgentFailureClusterDedup:
    """Receiver-side LRU dedup for AGENT_FAILURE_CLUSTER (Option C in
    profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md).

    Even with canonical-source emission at the producer side (Option A
    via canonical_agent_source), timing skew between the cron-emitter and
    mailbox-translator paths can fire two cluster events for the same
    canonical agent in the same 30-minute window before the shared
    detector state has converged. The receiver-side LRU is the
    belt-and-braces insurance: it suppresses Telegram delivery for
    ``(source, 30-min bucket)`` keys it has already sent, while leaving
    the bus event itself untouched (downstream consumers like the Critic
    substrate and audit logger still receive both copies).
    """

    def _cluster_event(self, source, timestamp):
        evt = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, source,
            {
                "source": source, "failure_type": "captcha", "count": 3,
                "first_seen": timestamp,
                "last_seen": timestamp,
            },
        )
        evt.timestamp = timestamp
        return evt

    def test_duplicate_cluster_same_bucket_is_suppressed(
        self, bus, topics_config, verbosity_config,
    ):
        """Two cluster events for the same source within the same 30-min
        bucket: the FIRST hits Telegram, the SECOND is suppressed.

        Without this dedup the cron-emitter and mailbox-translator paths
        each fire a cluster event for the same Applier exit-126 incident
        and the user sees two ``#watchdog_alerts`` messages back-to-back.
        """
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        evt1 = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        evt2 = self._cluster_event("applier", "2026-04-29T10:05:00+00:00")

        notifier.handle(evt1)
        notifier.handle(evt2)

        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1, (
            f"second cluster for same source in same 30-min bucket must "
            f"be suppressed; got {len(watchdog_deliveries)} deliveries: "
            f"{sent!r}"
        )

    def test_cluster_in_next_bucket_re_delivers(
        self, bus, topics_config, verbosity_config,
    ):
        """A cluster for the same agent in the NEXT 30-min bucket must
        deliver again — the dedup is rate-limit, not permanent
        suppression. Without this, an Applier failure that recurs the
        next morning would silently never re-alert."""
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        evt1 = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        evt2 = self._cluster_event("applier", "2026-04-29T10:31:00+00:00")

        notifier.handle(evt1)
        notifier.handle(evt2)

        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 2, (
            f"expected 2 deliveries across 2 buckets; got "
            f"{len(watchdog_deliveries)}: {sent!r}"
        )

    def test_different_sources_same_bucket_both_deliver(
        self, bus, topics_config, verbosity_config,
    ):
        """Dedup keys on (source, bucket). Different agents in the same
        time window are independent incidents; both must deliver."""
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        evt_a = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        evt_b = self._cluster_event("scout", "2026-04-29T10:05:00+00:00")

        notifier.handle(evt_a)
        notifier.handle(evt_b)

        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 2, (
            f"different sources must both deliver; got {sent!r}"
        )

    def test_dedup_does_not_affect_other_event_types(
        self, bus, topics_config, verbosity_config,
    ):
        """LRU dedup is scoped to AGENT_FAILURE_CLUSTER only. A CRON_FAILED
        event from the same source in the same window MUST still deliver —
        the dedup must not bleed across event types."""
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        cluster = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        cron_failed = Event.create(
            EventType.CRON_FAILED, "applier",
            {"job_id": "j", "job_name": "applier", "duration": 1.0,
             "error": "captcha", "consecutive_errors": 1},
        )

        notifier.handle(cluster)
        notifier.handle(cron_failed)

        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 2, (
            f"cron_failed must deliver after a cluster from same source; "
            f"got {sent!r}"
        )

    def test_lru_evicts_oldest_when_capacity_exceeded(
        self, bus, topics_config, verbosity_config,
    ):
        """LRU is bounded at CLUSTER_DEDUP_LRU_SIZE entries. After
        SIZE+1 unique (source, bucket) pairs, the oldest must have been
        evicted; re-emitting it should deliver again because the cache
        lost that key. Guards against unbounded memory growth in a
        long-running notifier."""
        from events.subscribers.telegram_notifier import CLUSTER_DEDUP_LRU_SIZE
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        first = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        notifier.handle(first)

        # Fill the LRU with SIZE distinct (source, bucket) keys so the
        # 'applier@bucket0' key gets evicted.
        for i in range(CLUSTER_DEDUP_LRU_SIZE):
            evt = self._cluster_event(
                f"agent-{i}", "2026-04-29T10:00:00+00:00",
            )
            notifier.handle(evt)

        # Re-emit the original key — should deliver again because evicted.
        replay = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        notifier.handle(replay)

        applier_lines = [
            s for s in sent
            if s[0] == "100" and "applier" in s[1] and "agent-" not in s[1]
        ]
        assert len(applier_lines) == 2, (
            f"after LRU eviction, replay must deliver again; "
            f"applier deliveries: {applier_lines!r}"
        )

    def test_canonical_source_dedups_cron_and_mailbox_paths(
        self, bus, topics_config, verbosity_config, tmp_path, monkeypatch,
    ):
        """End-to-end Option A + C check: when the cron-emitter path
        ('jobflow-applier' → canonical 'applier') and the
        mailbox-translator path ('applier') BOTH manage to push a
        cluster event into the bus before the shared detector state
        converges, the receiver-side LRU collapses them so the user
        sees ONE Telegram alert instead of two.

        This is the failure mode the proposal exists to close. Without
        canonicalisation (Option A), the cron path's source ON the bus
        event is 'applier' (post-mapping) and the mailbox path's source
        is also 'applier', so the LRU key collides cleanly. Without the
        LRU (Option C), simultaneous emission across the two paths still
        produces two cluster events and two Telegram alerts, defeating
        the dedup.
        """
        from events.producers.cron_emitter import CronEventEmitter
        from events.subscribers.mailbox_translator import MailboxTranslator

        # Shared detector state path so the two producers genuinely share
        # the same window (the production gateway wires both to
        # events.paths.failure_cluster_state_path).
        state_path = tmp_path / "events" / "failure_cluster_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "events.producers.cron_emitter.failure_cluster_state_path",
            lambda: state_path,
        )
        monkeypatch.setattr(
            "events.subscribers.mailbox_translator.failure_cluster_state_path",
            lambda: state_path,
        )

        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        emitter = CronEventEmitter(bus)
        translator = MailboxTranslator(bus)

        # Push the count above threshold from BOTH paths so each
        # invocation of record() returns ClusterInfo (the third entry
        # crosses 3, the fourth still satisfies "last 3 same type").
        for i in range(3):
            emitter.on_job_completed(
                job_id=f"j{i}", job_name="jobflow-applier",
                success=False, duration=1.0, error="captcha",
                consecutive_errors=i + 1,
            )
        translator._record_error_for_clustering(
            outer_payload={"from": "applier", "to": "main"},
            inner={"message": "captcha", "source_agent": "applier"},
            correlation_id=None,
        )

        # All cluster events on the bus carry the canonical source
        # 'applier' (Option A). The 4-record sequence above produces 2
        # cluster events: one when the cron path crossed 3, one when
        # the mailbox path added a 4th still-same-type entry.
        clusters = bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)
        assert len(clusters) >= 2, (
            f"expected >=2 bus cluster events across cron+mailbox paths; "
            f"got {len(clusters)}: {[c.source for c in clusters]}"
        )
        assert all(c.source == "applier" for c in clusters), (
            f"all cluster events must carry canonical source 'applier'; "
            f"got {[c.source for c in clusters]}"
        )

        for evt in clusters:
            notifier.handle(evt)

        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1, (
            f"Option A+C must deliver exactly 1 cluster Telegram alert "
            f"despite {len(clusters)} bus events; got {sent!r}"
        )

    def test_bus_event_query_still_records_duplicate(
        self, bus, topics_config, verbosity_config,
    ):
        """The dedup gate suppresses Telegram delivery only — the event
        bus history must still record both copies so downstream
        consumers (Critic substrate, audit-logger) can act on them. This
        guards the proposal's explicit "bus events stay distinct"
        requirement."""
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        # Emit through the bus (not just via notifier.handle) so query() finds them.
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER, source="applier",
            payload={"source": "applier", "failure_type": "captcha", "count": 3,
                     "first_seen": "2026-04-29T10:00:00+00:00",
                     "last_seen": "2026-04-29T10:00:00+00:00"},
        )
        bus.emit(
            event_type=EventType.AGENT_FAILURE_CLUSTER, source="applier",
            payload={"source": "applier", "failure_type": "captcha", "count": 3,
                     "first_seen": "2026-04-29T10:05:00+00:00",
                     "last_seen": "2026-04-29T10:05:00+00:00"},
        )
        # Drive both events through the notifier so it can dedup.
        for evt in bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER):
            notifier.handle(evt)

        # Bus retains both copies (audit / Critic still see the duplicate).
        assert len(bus.query(event_type=EventType.AGENT_FAILURE_CLUSTER)) == 2
        # Telegram side: only the first one delivers.
        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1


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
