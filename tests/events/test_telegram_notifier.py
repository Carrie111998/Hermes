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

    def test_credential_loss_routes_to_security_and_system(self):
        """R70 alert-gap fix (2026-07-10): a named credential/infra loss lands in
        the Security & System topic Diego watches. CRITICAL priority => survives
        significant_only and delivers even during quiet hours (the reliable 3am
        channel when WhatsApp itself is the lost credential)."""
        assert TOPIC_ROUTING["credential_loss"] == "security_and_system"

    def test_devflow_pr_events_route_to_devflow_firehose(self):
        """DevFlow PR activity (opened, closed, merged) belongs in the
        devflow_firehose topic alongside the existing devflow.run_*
        events. Added 2026-04-30 — spec
        docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
        Without this routing, PR events would fall through to ``system``
        and disappear from the user's SDLC monitoring stream.
        """
        assert TOPIC_ROUTING["devflow.pr_opened"] == "devflow_firehose"
        assert TOPIC_ROUTING["devflow.pr_merged"] == "devflow_firehose"
        assert TOPIC_ROUTING["devflow.pr_closed"] == "devflow_firehose"

    def test_devflow_pr_review_requested_routes_to_devflow_decisions(self):
        """PR ready-for-review is a human-action signal (someone needs
        to review the PR) — pair it with devflow_decisions, NOT firehose.
        Mirrors how jobflow_decisions carries human-action signals
        (interview_signal, offer_signal) while jobflow_firehose carries
        ambient activity. The Critic and Watchdog can also subscribe to
        devflow_decisions for proposal triage.
        """
        assert TOPIC_ROUTING["devflow.pr_review_requested"] == "devflow_decisions"

    def test_devflow_build_events_route_to_devflow_firehose(self):
        """Build started/succeeded land in firehose (ambient SDLC stream).
        Build LOW-priority started events get batched per the existing
        low-priority batching path; succeeded events at NORMAL deliver
        immediately.
        """
        assert TOPIC_ROUTING["devflow.build_started"] == "devflow_firehose"
        assert TOPIC_ROUTING["devflow.build_succeeded"] == "devflow_firehose"

    def test_devflow_build_failed_routes_to_devflow_decisions(self):
        """Build failures are decisions (someone needs to investigate).
        Routes to devflow_decisions like pr_review_requested. CROSS_POST_
        TO_ALERTS is intentionally NOT enabled for build_failed — DevFlow
        has its own decision topic and we don't want every red CI to
        leak into watchdog_alerts (already noisy with cron failures).
        """
        assert TOPIC_ROUTING["devflow.build_failed"] == "devflow_decisions"

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


class TestStatusBlackoutSelfDegradedRouting:
    """watchdog_self_degraded normally routes to watchdog_alerts, but the
    'monitoring has gone dark' reasons (status.json stale / unreadable) route
    to security_and_system instead.

    2026-07-13 incident: a 3h16m prober blackout's one HIGH status-stale alert
    was buried under gateway_health / WhatsApp flap on watchdog_alerts and went
    unactioned for 3h. security_and_system (thread 106 in the fixture, 9680 in
    prod) is the low-traffic operator topic (credential_loss, secret_detected,
    backend_contract_drift already land there) so the monitoring-dark alert is
    actually seen. The LaptopMonitor-Canary scheduled task is the primary
    auto-fix; this reroute is the complementary visibility half.
    """

    def _event(self, reason):
        return Event.create(
            EventType.WATCHDOG_SELF_DEGRADED, "watchdog", {"reason": reason},
        )

    def test_status_stale_routes_to_security_and_system(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._event("laptop-monitor status.json stale"))
        assert target[2] == "106"  # security_and_system, NOT watchdog_alerts(100)

    def test_status_unreadable_routes_to_security_and_system(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._event("status.json unreadable"))
        assert target[2] == "106"

    def test_over_budget_reason_stays_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        # "monitor pass over budget" means the writer is ALIVE (just slow) --
        # a normal operator watchdog signal, so it stays on watchdog_alerts.
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._event("monitor pass over budget"))
        assert target[2] == "100"

    def test_self_degraded_without_reason_stays_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._event(""))
        assert target[2] == "100"

    def test_status_blackout_not_cross_posted_to_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        # WATCHDOG_SELF_DEGRADED is HIGH priority but NOT in CROSS_POST_TO_ALERTS,
        # so the re-routed primary must be the ONLY target -- it must not also
        # cross-post back to the flap-saturated watchdog_alerts topic.
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        targets = notifier.resolve_all_targets(
            self._event("laptop-monitor status.json stale")
        )
        threads = [t[2] for t in targets]
        assert threads == ["106"]
        assert "100" not in threads


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

    def test_formats_resource_pressure_readably(
        self, bus, topics_config, verbosity_config,
    ):
        """RESOURCE_PRESSURE renders a single operator-readable line, not a
        raw dump of the nested ``thresholds`` dict (which the generic
        fallback would splat verbatim)."""
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.RESOURCE_PRESSURE, "system",
            {
                "reasons": ["commit_high", "pagefile_growth"],
                "commit_used_gb": 84.2,
                "commit_limit_gb": 85.6,
                "commit_pct": 98.4,
                "pagefile_allocated_gb": 54.4,
                "pagefile_growth_gb_10min": 18.0,
                "disk_c_free_gb": 12.3,
                "thresholds": {
                    "commit_pct": 85.0, "disk_free_gb": 15.0,
                    "pagefile_growth_gb": 2.0, "growth_window_min": 10.0,
                },
            },
        )
        body = notifier._format_payload(event)
        assert "98.4" in body                          # commit %
        assert "84.2" in body and "85.6" in body       # commit used/limit
        assert "54.4" in body                           # pagefile alloc
        assert "12.3" in body                           # C: free
        assert "commit_high" in body and "pagefile_growth" in body
        # The raw thresholds dict must NOT leak into the message.
        assert "growth_window_min" not in body
        assert "thresholds" not in body

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
        and the user sees two ``#jobflow_firehose`` messages back-to-back
        (JobFlow-agent clusters route there since 2026-07-16).
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

        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 1, (
            f"second cluster for same source in same 30-min bucket must "
            f"be suppressed; got {len(firehose_deliveries)} deliveries: "
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

        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 2, (
            f"expected 2 deliveries across 2 buckets; got "
            f"{len(firehose_deliveries)}: {sent!r}"
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

        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 2, (
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

        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 2, (
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
            if s[0] == "101" and "applier" in s[1] and "agent-" not in s[1]
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

        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 1, (
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
        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 1


class TestWatchdogDailySummary:
    """WATCHDOG_DAILY — once-per-day aggregate health heartbeat (2026-04-30).

    Diego's request (visibility-restoration session B9): "Watchdog: daily
    digest and also firing when something breaks." The per-failure events
    (watchdog_silence_alert, watchdog_burst, watchdog_self_degraded,
    watchdog_recovered, watchdog_probe_transition) already carry the "fire
    when something breaks" half. WATCHDOG_DAILY is the missing 7am ET
    heartbeat that surfaces aggregate health (probes_total, healthy,
    degraded, down, escalations_24h, stale_probes) so a quiet feed means
    "Watchdog is alive and reports green," not "Watchdog might be dead."

    Routing + verbosity contract:
      - Routes to watchdog_alerts (alongside the other watchdog signals).
      - Default priority NORMAL — failure-fires (HIGH+) keep their own
        gate; the daily heartbeat must NOT be NORMAL-batched as low-prio
        chatter and must NOT impersonate a HIGH alert.
      - Verbosity ``significant_only`` (the existing watchdog_alerts mode)
        drops it — by design; that mode is for incidents only.
      - Verbosity ``digest_only`` passes it — that mode is the existing
        symmetric option for users who want HIGH+ failure-fires AND the
        daily heartbeat without LOW/NORMAL chatter. Pre-2026-04-30 the
        ``digest_only`` branch was a code-level duplicate of
        ``significant_only``; this test suite pins the digest-class
        pass-through that gives ``digest_only`` distinct semantics.
    """

    def _daily_event(self, payload=None):
        return Event.create(
            EventType.WATCHDOG_DAILY, "watchdog",
            payload or {
                "probes_total": 67, "healthy": 66, "degraded": 0, "down": 1,
                "escalations_24h": 5, "stale_probes": ["postgres-sync"],
            },
        )

    def test_event_type_exists_with_normal_priority(self):
        """WATCHDOG_DAILY must be a first-class EventType at NORMAL priority.

        NORMAL (not HIGH) so the heartbeat doesn't impersonate an incident
        and trigger downstream WhatsApp escalation tiers. NOT LOW so it
        doesn't get swept into the 5-minute batched-chatter buffer where a
        once-a-day heartbeat would be paired with unrelated low-prio
        events.
        """
        et = EventType.WATCHDOG_DAILY
        assert et.type_string == "watchdog_daily"
        assert et.default_priority == Priority.NORMAL

    def test_routes_to_watchdog_alerts(self):
        """TOPIC_ROUTING must place WATCHDOG_DAILY in watchdog_alerts.

        Pin the routing alongside the other watchdog signals so a future
        cutover that splits watchdog topics has to update one obvious place.
        """
        assert TOPIC_ROUTING["watchdog_daily"] == "watchdog_alerts"

    def test_has_emoji(self):
        """WATCHDOG_DAILY must have a distinct EVENT_TYPE_EMOJI entry.

        A missing entry makes event_icon() return "" and the header
        renders with a double-space gap (SR-408 regression pattern,
        2026-04-19). Operators scanning watchdog_alerts need a visual
        token that distinguishes the daily heartbeat from the per-failure
        signals (💓 tick, 🔄 transition, 🌊 burst, 🔕 silence, 🤕 degraded,
        💚 recovered).
        """
        from events.formatting import EVENT_TYPE_EMOJI
        icon = EVENT_TYPE_EMOJI.get(EventType.WATCHDOG_DAILY, "")
        assert icon, "WATCHDOG_DAILY missing from EVENT_TYPE_EMOJI"

    def test_passes_under_all_verbosity(self, bus, topics_config, tmp_path):
        """verbosity=all: NORMAL daily digest passes through unfiltered."""
        verbosity_path = tmp_path / "telegram" / "verbosity.json"
        verbosity_path.parent.mkdir(parents=True, exist_ok=True)
        verbosity_path.write_text(json.dumps(
            {"watchdog_alerts": {"mode": "all"}}
        ))
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_path,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        notifier.handle(self._daily_event())
        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1, (
            f"verbosity=all must pass NORMAL WATCHDOG_DAILY; got {sent!r}"
        )

    def test_dropped_under_significant_only_verbosity(
        self, bus, topics_config, tmp_path,
    ):
        """verbosity=significant_only: NORMAL daily digest is dropped.

        This is by design — significant_only is the "incidents only" mode.
        The daily heartbeat is not an incident.
        """
        verbosity_path = tmp_path / "telegram" / "verbosity.json"
        verbosity_path.parent.mkdir(parents=True, exist_ok=True)
        verbosity_path.write_text(json.dumps(
            {"watchdog_alerts": {"mode": "significant_only"}}
        ))
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_path,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        notifier.handle(self._daily_event())
        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 0, (
            f"verbosity=significant_only must drop NORMAL WATCHDOG_DAILY; "
            f"got {sent!r}"
        )

    def test_passes_under_digest_only_verbosity(
        self, bus, topics_config, tmp_path,
    ):
        """verbosity=digest_only: WATCHDOG_DAILY passes regardless of priority.

        Pre-2026-04-30 the ``digest_only`` branch was a code-level
        duplicate of ``significant_only`` (both gated NORMAL/LOW out and
        both let HIGH+ through). That made ``digest_only`` indistinguishable
        from ``significant_only`` for a topic, so an operator who set their
        watchdog_alerts to ``digest_only`` got the same incident stream and
        no daily summary.

        Post-2026-04-30 ``digest_only`` is the symmetric "HIGH+ AND
        digest-class events" mode: failure-fires still pass (HIGH+) AND
        the daily heartbeat passes (digest-class), but routine NORMAL/LOW
        chatter still drops.
        """
        verbosity_path = tmp_path / "telegram" / "verbosity.json"
        verbosity_path.parent.mkdir(parents=True, exist_ok=True)
        verbosity_path.write_text(json.dumps(
            {"watchdog_alerts": {"mode": "digest_only"}}
        ))
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_path,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        notifier.handle(self._daily_event())
        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1, (
            f"verbosity=digest_only must pass WATCHDOG_DAILY; got {sent!r}"
        )

    def test_digest_only_still_passes_high_priority_failure_fires(
        self, bus, topics_config, tmp_path,
    ):
        """verbosity=digest_only: HIGH+ failure-fires must still pass.

        The new digest_only semantics are additive — they let digest-class
        events through in addition to HIGH+. They must not regress the
        existing HIGH+ pass-through; otherwise an operator switching from
        significant_only to digest_only would silently lose all incident
        alerts.
        """
        verbosity_path = tmp_path / "telegram" / "verbosity.json"
        verbosity_path.parent.mkdir(parents=True, exist_ok=True)
        verbosity_path.write_text(json.dumps(
            {"watchdog_alerts": {"mode": "digest_only"}}
        ))
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_path,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        # WATCHDOG_BURST is HIGH-priority (failure-fire archetype)
        burst = Event.create(
            EventType.WATCHDOG_BURST, "watchdog",
            {"count": 3, "trigger": "burst_threshold", "transitions": []},
            priority=Priority.HIGH,
        )
        notifier.handle(burst)
        watchdog_deliveries = [s for s in sent if s[0] == "100"]
        assert len(watchdog_deliveries) == 1, (
            f"verbosity=digest_only must keep HIGH+ failure-fires passing; "
            f"got {sent!r}"
        )

    def test_digest_only_drops_unrelated_normal_priority_event(
        self, bus, topics_config, tmp_path,
    ):
        """verbosity=digest_only: NORMAL events that aren't digest-class
        still drop.

        digest_only is permissive for digest-class events ONLY (curator_daily,
        watchdog_daily, digest_generated). A generic NORMAL event must
        still be filtered, otherwise digest_only collapses into ``all``
        and the "no LOW/NORMAL chatter" promise is broken.

        We use STAGE_TRANSITION (NORMAL priority by default) routed
        elsewhere, but emit it through a topic configured digest_only and
        verify it drops. To test this cleanly we need an event whose
        primary topic is set to digest_only; we route through a synthetic
        digest_only setting on jobflow_firehose.
        """
        verbosity_path = tmp_path / "telegram" / "verbosity.json"
        verbosity_path.parent.mkdir(parents=True, exist_ok=True)
        verbosity_path.write_text(json.dumps(
            {"jobflow_firehose": {"mode": "digest_only"}}
        ))
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_path,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        # STAGE_TRANSITION → jobflow_firehose, NORMAL, not digest-class
        evt = Event.create(
            EventType.STAGE_TRANSITION, "tracker",
            {"prior_stage": "discovered", "new_stage": "applied"},
            priority=Priority.NORMAL,
        )
        notifier.handle(evt)
        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(firehose_deliveries) == 0, (
            f"verbosity=digest_only must drop non-digest NORMAL events; "
            f"got {sent!r}"
        )


def test_watchdog_burst_routes_to_watchdog_alerts_topic():
    """Coalesced burst events go to the same topic as single-probe transitions."""
    from events.subscribers.telegram_notifier import TOPIC_ROUTING

    assert TOPIC_ROUTING.get("watchdog_burst") == "watchdog_alerts"


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


class TestNotificationDeliveredReverseSignal:
    """NOTIFICATION_DELIVERED + NOTIFICATION_FAILED reverse-signal layer
    (2026-04-30, design at docs/superpowers/specs/2026-04-30-notification-
    delivered-design.md).

    The bus is one-way today: events emit -> telegram_notifier delivers
    via Telegram bridge -> nothing flows back. These tests pin the
    reverse signal in TelegramNotifier._deliver(): on success emit a
    LOW NOTIFICATION_DELIVERED carrying original_event_id, platform,
    target, latency_ms; on failure emit a NORMAL NOTIFICATION_FAILED
    with error.kind + error.message.

    Cycle prevention: the subscriber MUST NOT consume its own delivery
    events (would loop forever on every send). Tests pin the early-
    return guard in handle().

    Volume scoping: only non-batched (>= NORMAL priority) deliveries
    emit reverse signals — LOW-priority batched deliveries do NOT, so
    cron firehose chatter doesn't double the bus volume. Failures are
    always emitted regardless of priority (operator must see them).
    """

    def test_emits_notification_delivered_on_success(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,  # success
        )
        original = Event.create(
            EventType.JOB_SCORED, "matcher",
            {"score": 7.5, "title": "Engineer", "company": "Beta"},
            priority=Priority.NORMAL,
        )
        original_id = bus.emit(
            event_type=EventType.JOB_SCORED, source="matcher",
            payload=original.payload, priority=Priority.NORMAL,
        )
        original.event_id = original_id  # keep test event in sync with bus

        notifier.handle(original)

        delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
        assert len(delivered) == 1, (
            f"expected exactly one NOTIFICATION_DELIVERED, got {len(delivered)}"
        )
        evt = delivered[0]
        assert evt.priority == Priority.LOW
        assert evt.payload["original_event_id"] == original_id
        assert evt.payload["original_event_type"] == "job_scored"
        assert evt.payload["platform"] == "telegram"
        assert evt.payload["target"]["chat_id"] == "-1001234567890"
        assert evt.payload["target"]["thread_id"] == "101"  # jobflow_firehose
        assert evt.payload["target"]["topic_key"] == "jobflow_firehose"
        assert evt.payload["latency_ms"] >= 0
        assert evt.correlation_id == original_id

    def test_emits_notification_failed_on_exception(
        self, bus, topics_config, verbosity_config,
    ):
        def boom(chat_id, thread_id, msg):
            raise RuntimeError("Bad Request: chat not found")

        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=boom,
        )
        original_id = bus.emit(
            event_type=EventType.JOB_SCORED, source="matcher",
            payload={"score": 7.5, "title": "Engineer", "company": "Beta"},
            priority=Priority.NORMAL,
        )
        # Recreate the Event object as handle() would receive it
        original = bus.query(event_type=EventType.JOB_SCORED)[0]

        # _deliver() catches the exception per the contract (non-raising
        # wrapper around the send_fn). The reverse signal must still fire.
        notifier.handle(original)

        failed = bus.query(event_type=EventType.NOTIFICATION_FAILED)
        assert len(failed) == 1, (
            f"expected exactly one NOTIFICATION_FAILED, got {len(failed)}"
        )
        evt = failed[0]
        assert evt.priority == Priority.NORMAL
        assert evt.payload["original_event_id"] == original_id
        assert evt.payload["platform"] == "telegram"
        assert evt.payload["error"]["kind"] == "RuntimeError"
        assert "Bad Request" in evt.payload["error"]["message"]

    def test_emit_failure_does_not_break_delivery(
        self, bus, topics_config, verbosity_config, monkeypatch,
    ):
        """If bus.emit raises while emitting the reverse signal, the
        actual delivery must still complete and no exception bubbles out
        of handle(). Production failure mode: a transient SQLite lock on
        event_bus.db must not silence legit notifications.
        """
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )
        original_id = bus.emit(
            event_type=EventType.JOB_SCORED, source="matcher",
            payload={"score": 7.5, "title": "X", "company": "Y"},
            priority=Priority.NORMAL,
        )
        original = bus.query(event_type=EventType.JOB_SCORED)[0]

        # Break only the reverse-signal emit by replacing bus.emit with a
        # raising version AFTER the original event was emitted by the
        # producer above. The notifier's _deliver() will call bus.emit and
        # the wrapper must swallow.
        original_emit = bus.emit
        emit_calls = {"count": 0}

        def maybe_raising_emit(*args, **kwargs):
            emit_calls["count"] += 1
            raise RuntimeError("event_bus locked")

        monkeypatch.setattr(bus, "emit", maybe_raising_emit)

        # Must not raise. Send must succeed.
        notifier.handle(original)

        assert len(sent) == 1, "delivery must complete despite emit failure"
        assert emit_calls["count"] >= 1, (
            "_deliver() must have attempted the reverse-signal emit"
        )

    def test_does_not_consume_own_notification_delivered_events(
        self, bus, topics_config, verbosity_config,
    ):
        """Cycle guard: NOTIFICATION_DELIVERED events feeding back into
        handle() must short-circuit before reaching resolve_target(),
        the batch buffer, or _deliver(). Without this, every successful
        send would feed a delivery event back through the same subscriber
        and recurse (LOW priority would batch and eventually flush a
        message about a delivery, triggering another NOTIFICATION_
        DELIVERED about delivering the delivery message — same loop).
        """
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )
        delivered_event = Event.create(
            EventType.NOTIFICATION_DELIVERED, "telegram-notifier",
            {
                "original_event_id": "abc-123",
                "platform": "telegram",
                "target": {"chat_id": "-1", "thread_id": "101"},
                "latency_ms": 10,
            },
            priority=Priority.LOW,
        )
        notifier.handle(delivered_event)

        assert sent == [], "subscriber must not deliver its own delivery events"
        # Cycle guard must short-circuit BEFORE the batch buffer too, otherwise
        # a 5-min flush would still ship a "we delivered" message to a topic
        # and then emit another NOTIFICATION_DELIVERED for that send.
        assert notifier._batch_buffer == {}, (
            f"cycle guard must skip batching too; got {notifier._batch_buffer}"
        )
        # And nothing on the bus emitted by us
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []

    def test_does_not_consume_own_notification_failed_events(
        self, bus, topics_config, verbosity_config,
    ):
        """Cycle guard: NOTIFICATION_FAILED at NORMAL priority would
        otherwise route to watchdog_alerts (per defensive TOPIC_ROUTING)
        and trigger ANOTHER send -> if THAT failed, another
        NOTIFICATION_FAILED -> recursion until SQLite locks up.
        """
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )
        failed_event = Event.create(
            EventType.NOTIFICATION_FAILED, "telegram-notifier",
            {
                "original_event_id": "abc-123",
                "platform": "telegram",
                "target": {"chat_id": "-1", "thread_id": "101"},
                "latency_ms": 50,
                "error": {"kind": "RuntimeError", "message": "timeout"},
            },
            priority=Priority.NORMAL,
        )
        notifier.handle(failed_event)

        assert sent == [], "subscriber must not retry its own failure events"
        assert bus.query(event_type=EventType.NOTIFICATION_FAILED) == []

    def test_cross_post_emits_two_delivered_events(
        self, bus, topics_config, verbosity_config,
    ):
        """interview_signal at HIGH cross-posts to watchdog_alerts (per
        CROSS_POST_TO_ALERTS). Each target sends one message and emits
        one NOTIFICATION_DELIVERED, so the audit log can answer "did
        Telegram show this in BOTH topics?" per topic_key.
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,
        )
        original_id = bus.emit(
            event_type=EventType.INTERVIEW_SIGNAL, source="tracker",
            payload={"company": "Acme", "detail": "phone screen scheduled"},
            priority=Priority.CRITICAL,  # interview_signal default is CRITICAL
        )
        original = bus.query(event_type=EventType.INTERVIEW_SIGNAL)[0]
        notifier.handle(original)

        delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
        assert len(delivered) == 2, (
            f"expected 2 NOTIFICATION_DELIVERED (primary + cross-post); "
            f"got {len(delivered)}"
        )
        thread_ids = {d.payload["target"]["thread_id"] for d in delivered}
        # Primary jobflow_decisions = 102; cross-post watchdog_alerts = 100
        assert thread_ids == {"102", "100"}, (
            f"unexpected thread_ids in delivery events: {thread_ids}"
        )
        for d in delivered:
            assert d.payload["original_event_id"] == original_id
            assert d.correlation_id == original_id

    def test_low_priority_batched_delivery_does_not_emit_per_event(
        self, bus, topics_config, verbosity_config,
    ):
        """LOW-priority events are buffered into the 5-min batch window
        (one combined send later); per-event reverse signals would
        double bus volume on the firehose. Phase 1 scoping: LOW deliveries
        emit no reverse signal. Failures (when batches eventually flush)
        will still emit per-platform (covered by a separate test).
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,
        )
        bus.emit(
            event_type=EventType.JOB_DISCOVERED, source="scout",
            payload={"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        )
        original = bus.query(event_type=EventType.JOB_DISCOVERED)[0]
        notifier.handle(original)

        # Buffered, not delivered yet
        assert len(notifier._batch_buffer) > 0
        # No reverse signal for the batched (yet-to-flush) event
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []


class TestSynthesizedIterationSuppression:
    """Synthesized AGENT_ITERATION placeholders (scheduler marker-missing
    fallback) are bus-only telemetry — never delivered to chat
    (2026-07-11 comms audit: devflow-bridge emitted ~918/day of these)."""

    def _make(self, synthesized: bool):
        payload = {"agent": "devflow", "summary": "devflow completed"}
        if synthesized:
            payload["synthesized"] = True
        return Event.create(EventType.AGENT_ITERATION, "devflow", payload)

    def test_synthesized_iteration_never_reaches_chat(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda *a, **k: sent.append(a),
        )
        notifier.handle(self._make(synthesized=True))
        assert sent == []
        assert all(not msgs for msgs in notifier._batch_buffer.values()), \
            "synthesized iteration must not even be batched"

    def test_real_iteration_still_flows(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda *a, **k: None,
        )
        notifier.handle(self._make(synthesized=False))
        # AGENT_ITERATION is LOW priority => it lands in the batch buffer.
        assert any(msgs for msgs in notifier._batch_buffer.values())


class TestCronLifecycleRouting:
    """2026-07-11 comms-audit split: routine cron lifecycle -> cron_firehose;
    failure modes stay on watchdog_alerts."""

    @pytest.fixture
    def topics_with_cron_firehose(self, tmp_path):
        config = {
            "group_chat_id": "-1001234567890",
            "topics": {
                "watchdog_alerts": {"thread_id": 100, "name": "Watchdog Alerts"},
                "cron_firehose": {"thread_id": 110, "name": "Cron Firehose"},
            },
        }
        path = tmp_path / "telegram" / "topics_cron.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        return path

    def test_cron_lifecycle_routes_to_cron_firehose(
        self, bus, topics_with_cron_firehose, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_with_cron_firehose,
            verbosity_path=verbosity_config,
        )
        for et in (
            EventType.CRON_STARTED, EventType.CRON_COMPLETED,
            EventType.CRON_SKIPPED, EventType.CRON_TRIGGERED,
            EventType.CRON_SKIPPED_DUPLICATE,
            EventType.CRON_SKIPPED_MIN_INTERVAL,
        ):
            ev = Event.create(et, "postgres-sync", {"job_name": "postgres-sync"})
            assert notifier.resolve_target(ev)[2] == "110", \
                f"{et.type_string} should route to cron_firehose"

    def test_cron_failures_stay_on_watchdog_alerts(
        self, bus, topics_with_cron_firehose, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_with_cron_firehose,
            verbosity_path=verbosity_config,
        )
        for et in (
            EventType.CRON_FAILED, EventType.CRON_FAILED_CONSECUTIVE,
            EventType.CRON_STALE,
        ):
            ev = Event.create(et, "postgres-sync", {"job_name": "postgres-sync"})
            assert notifier.resolve_target(ev)[2] == "100", \
                f"{et.type_string} should stay on watchdog_alerts"

    def test_missing_cron_firehose_falls_back_to_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        # topics_config fixture predates the cron_firehose topic — cron
        # telemetry must degrade to watchdog_alerts, not leak to General
        # via an empty thread_id.
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        ev = Event.create(EventType.CRON_COMPLETED, "postgres-sync", {})
        assert notifier.resolve_target(ev)[2] == "100"


class TestJobflowFailureRouting:
    """2026-07-16 operator request: failure events sourced from JobFlow
    pipeline agents (applier, tracker, scout, ...) belong in jobflow_firehose,
    not watchdog_alerts — their errors are pipeline telemetry, and burying
    them among infrastructure alerts drowns the operator stream. System-
    sourced failures (postgres-sync, jaum, watchdog itself) stay on
    watchdog_alerts. AGENT_FAILURE_CLUSTER keeps its WhatsApp escalation
    path regardless of Telegram topic.
    """

    def _notifier(self, bus, topics_path, verbosity_path):
        return TelegramNotifier(
            bus, topics_path=topics_path, verbosity_path=verbosity_path,
        )

    def test_agent_error_from_jobflow_agent_routes_to_jobflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        for agent in ("applier", "tracker", "scout"):
            ev = Event.create(
                EventType.AGENT_ERROR, f"mailbox:{agent}",
                {"message": "boom", "source_agent": agent},
            )
            assert notifier.resolve_target(ev)[2] == "101", \
                f"agent_error from {agent} should route to jobflow_firehose"

    def test_agent_error_falls_back_to_event_source_when_payload_missing(
        self, bus, topics_config, verbosity_config,
    ):
        # mailbox: transport prefix on event.source must be stripped before
        # the agent lookup when payload.source_agent is absent.
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.AGENT_ERROR, "mailbox:applier", {"message": "boom"},
        )
        assert notifier.resolve_target(ev)[2] == "101"

    def test_agent_error_from_non_jobflow_agent_stays_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        for agent in ("jaum", "watchdog", "postgres-sync", "mempalace"):
            ev = Event.create(
                EventType.AGENT_ERROR, f"mailbox:{agent}",
                {"message": "boom", "source_agent": agent},
            )
            assert notifier.resolve_target(ev)[2] == "100", \
                f"agent_error from {agent} should stay on watchdog_alerts"

    def test_failure_cluster_for_jobflow_agent_routes_to_jobflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, "applier",
            {"source": "applier", "count": 3},
        )
        assert notifier.resolve_target(ev)[2] == "101"

    def test_failure_cluster_for_system_agent_stays_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, "postgres-sync",
            {"source": "postgres-sync", "count": 3},
        )
        assert notifier.resolve_target(ev)[2] == "100"

    def test_jobflow_cron_failures_route_to_jobflow_firehose(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        for et in (
            EventType.CRON_FAILED, EventType.CRON_FAILED_CONSECUTIVE,
            EventType.CRON_STALE,
        ):
            for job in ("jobflow-tracker-cycle", "jobflow-applier",
                        "sentinel-vip-evening"):
                ev = Event.create(et, job, {"job_name": job})
                assert notifier.resolve_target(ev)[2] == "101", \
                    f"{et.type_string} from {job} should route to jobflow_firehose"

    def test_jobflow_prefixed_cron_without_canonical_agent_still_reroutes(
        self, bus, topics_config, verbosity_config,
    ):
        # 'jobflow-approved-release' canonicalises to 'approved' (not a
        # known agent) but the jobflow- prefix alone marks it as pipeline.
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.CRON_FAILED, "jobflow-approved-release",
            {"job_name": "jobflow-approved-release"},
        )
        assert notifier.resolve_target(ev)[2] == "101"

    def test_main_agent_failures_stay_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        # 'main' maps to jobflow_firehose for AGENT_ITERATION chatter, but
        # its FAILURES are Hermes-core signals — keep them operator-visible.
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.AGENT_ERROR, "mailbox:main",
            {"message": "boom", "source_agent": "main"},
        )
        assert notifier.resolve_target(ev)[2] == "100"

    def test_missing_jobflow_firehose_topic_falls_back_to_watchdog_alerts(
        self, bus, tmp_path, verbosity_config,
    ):
        # topics.json without jobflow_firehose must degrade to
        # watchdog_alerts, not leak to General via an empty thread_id.
        config = {
            "group_chat_id": "-1001234567890",
            "topics": {
                "watchdog_alerts": {"thread_id": 100, "name": "Watchdog Alerts"},
            },
        }
        path = tmp_path / "telegram" / "topics_no_firehose.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config))
        notifier = self._notifier(bus, path, verbosity_config)
        ev = Event.create(
            EventType.CRON_FAILED, "jobflow-applier",
            {"job_name": "jobflow-applier"},
        )
        assert notifier.resolve_target(ev)[2] == "100"
