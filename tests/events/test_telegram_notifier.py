"""Tests for events.subscribers.telegram_notifier — Telegram forum topic routing."""

import json
from unittest.mock import patch

import pytest

from events.bus import EventBus
from events.routing_policy import _POLICY, classify
from events.schema import Event, EventType, Priority
from events.subscribers.telegram_notifier import TelegramNotifier


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


NO_WORK_BRIEF = (
    "── Critic · daily skill review ──\n"
    "VERDICT: No changes recommended.\n"
    "\n"
    "REVIEWED: 20 skills, 7 days of evidence (28,796 audit lines)\n"
    "TOP REJECTED EVIDENCE:\n"
    "  orchestrator ×3 → DevFlow actor IDs, not skill use\n"
    "SCORES MOVED: none\n"
    "RETIREMENT FLAGS: none\n"
    "\n"
    "ACTION NEEDED: none"
)


class TestAgentIterationBriefFormatting:
    def _notifier(self, bus, topics_config, verbosity_config):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )

    def test_brief_rendered_verbatim_without_counters(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.AGENT_ITERATION, "critic",
            {
                "agent": "critic",
                "summary": "no changes",
                "brief": NO_WORK_BRIEF,
                "counters": {"skills_inspected": 20, "audit_lines_scanned": 28796},
            },
            priority=Priority.LOW,
        )
        body = notifier._format_payload(event)
        assert body == NO_WORK_BRIEF
        assert "skills_inspected=20" not in body
        assert not body.startswith("critic:")

    def test_brief_absent_falls_back_to_legacy(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.AGENT_ITERATION, "critic",
            {"agent": "critic", "summary": "no changes",
             "counters": {"skills_inspected": 20}},
            priority=Priority.LOW,
        )
        body = notifier._format_payload(event)
        assert body.startswith("critic: no changes")
        assert "skills_inspected=20" in body

    def test_brief_with_anomalies_appends_warning(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.AGENT_ITERATION, "critic",
            {"agent": "critic", "summary": "x", "brief": NO_WORK_BRIEF,
             "anomalies": [{"kind": "audit_gap", "note": "missing day"}]},
            priority=Priority.LOW,
        )
        body = notifier._format_payload(event)
        assert body.startswith(NO_WORK_BRIEF)
        assert "⚠" in body
        assert "audit_gap" in body


class TestTopicRouting:
    def test_all_event_types_have_routing(self):
        # v3: the event→topic table lives in events.routing_policy._POLICY
        # (single source of truth both delivery subscribers consult).
        # Collect-then-assert so the message reports the FULL drift; the
        # in-loop form named only the first miss. See events/coverage.py.
        missing = [et.type_string for et in EventType if et not in _POLICY]
        assert not missing, (
            f"{len(missing)} of {len(list(EventType))} EventType members "
            f"missing from routing_policy._POLICY: {', '.join(missing)}"
        )


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

    def test_critic_routes_to_critic_proposals_compatibility_alias(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("critic"))
        assert target[2] == "108"

    def test_decision_required_critic_has_one_action_required_target(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.CRITIC_PROPOSAL,
            "critic.proposal_bridge",
            {"summary": "Choose a fix", "decision_required": True},
        )

        targets = notifier.resolve_all_targets(event)

        assert targets == [("telegram", "-1001234567890", "102")]

    def test_curator_routes_to_agents_memory(
        self, bus, topics_config, verbosity_config,
    ):
        # v3: curator joined the agents_memory domain topic; the fixture
        # predates the cutover so it resolves via the critic_proposals alias
        # (same thread) rather than the retired curator_digest topic.
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        target = notifier.resolve_target(self._make_event("curator"))
        assert target[2] == "108"

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
        assert classify(Event.create(
            EventType.JOB_DISCOVERED, "scout", {})).topic_key == "jobflow_firehose"
        assert classify(Event.create(
            EventType.JOB_VIP_DISCOVERED, "scout", {})).topic_key == "jobflow_firehose"

    def test_critical_events_route_to_action_required(self):
        # v3 attention-first cutover: human-action signals (blocked
        # applications, interviews, offers) are ACT class and land in the
        # cross-domain action_required topic (thread-aliased onto
        # jobflow_decisions for pre-cutover topics.json files).
        assert classify(Event.create(
            EventType.APPLICATION_BLOCKED, "applier", {})).topic_key == "action_required"
        assert classify(Event.create(
            EventType.INTERVIEW_SIGNAL, "tracker", {})).topic_key == "action_required"
        assert classify(Event.create(
            EventType.OFFER_SIGNAL, "tracker", {})).topic_key == "action_required"

    def test_topic_routing_covers_all_domain_events(self):
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
        # v3: _POLICY is keyed by EventType directly.
        missing = required - set(_POLICY)
        assert not missing, f"routing_policy._POLICY missing: {missing}"

    def test_credential_loss_routes_to_action_required_at_critical(self):
        """R70 alert-gap fix (2026-07-10), v3 shape: a named credential/infra
        loss is an ACT signal — it lands in the operator's action_required
        topic at CRITICAL priority, so it survives significant_only verbosity
        and pages IMMEDIATE even during quiet hours (the reliable 3am channel
        when WhatsApp itself is the lost credential)."""
        route = classify(Event.create(EventType.CREDENTIAL_LOSS, "watchdog", {}))
        assert route.topic_key == "action_required"
        assert route.priority == Priority.CRITICAL
        assert route.wa_tier == "immediate"

    def test_devflow_pr_events_route_to_devflow_firehose(self):
        """DevFlow PR activity (opened, closed, merged) belongs in the
        devflow_firehose topic alongside the existing devflow.run_*
        events. Added 2026-04-30 — spec
        docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
        Without this routing, PR events would fall through to the alerts
        degrade path and disappear from the user's SDLC monitoring stream.
        """
        for et in (EventType.DEVFLOW_PR_OPENED, EventType.DEVFLOW_PR_MERGED,
                   EventType.DEVFLOW_PR_CLOSED):
            assert classify(Event.create(et, "devflow", {})).topic_key == \
                "devflow_firehose"

    def test_devflow_pr_review_requested_routes_to_action_required(self):
        """PR ready-for-review is a human-action signal (someone needs
        to review the PR) — v3 classifies it ACT, landing in the
        cross-domain action_required topic alongside the JobFlow
        human-action signals (interview_signal, offer_signal), while
        devflow_firehose keeps the ambient activity.
        """
        route = classify(Event.create(
            EventType.DEVFLOW_PR_REVIEW_REQUESTED, "devflow", {}))
        assert route.topic_key == "action_required"

    def test_devflow_build_events_route_to_devflow_firehose(self):
        """Build started/succeeded land in firehose (ambient SDLC stream).
        Both are TRACE class: below HIGH they batch per the existing
        low-priority batching path.
        """
        assert classify(Event.create(
            EventType.DEVFLOW_BUILD_STARTED, "devflow", {})).topic_key == \
            "devflow_firehose"
        assert classify(Event.create(
            EventType.DEVFLOW_BUILD_SUCCEEDED, "devflow", {})).topic_key == \
            "devflow_firehose"

    def test_devflow_build_failed_routes_to_watchdog_alerts(self):
        """Build failures are WARN class in v3 — something is broken, so
        they land on watchdog_alerts (the now flap-collapsed, noop-
        suppressed alert stream) rather than a devflow decision topic.
        They stay phone-worthy via the explicit URGENT WhatsApp pin.
        """
        route = classify(Event.create(
            EventType.DEVFLOW_BUILD_FAILED, "devflow", {}))
        assert route.topic_key == "watchdog_alerts"
        assert route.wa_tier == "urgent"

    def test_agent_failure_cluster_routes_to_watchdog_alerts(self):
        """agent_failure_cluster fires from the watchdog detector and is
        an operational alert (cluster of failures across agents). It routes
        to watchdog_alerts.

        Regression: 2026-04-26 — the pre-v3 TOPIC_ROUTING dict contained two
        entries for 'agent_failure_cluster' (one mapping to watchdog_alerts,
        one to critic_proposals). Python dict literals are last-write-wins, so
        the cluster events silently went only to critic_proposals;
        watchdog_alerts never received them.

        Why watchdog_alerts is the right primary topic: the event source is
        the watchdog detector, and v3 deliberately keeps systemic signals
        (clusters, consecutive failures) on the alert stream — they are
        page-worthy and must NOT demote to a domain firehose. The Critic
        also consumes the cluster but produces critic_proposal events as
        its output — and those route to agents_memory/critic_proposals.
        Trigger and proposal are separate events with separate topics.
        """
        assert classify(Event.create(
            EventType.AGENT_FAILURE_CLUSTER, "watchdog",
            {"source": "watchdog"})).topic_key == "watchdog_alerts"


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
        # v3 removed cross-posting entirely (one event, one message), so the
        # re-routed primary must be the ONLY target -- it must not also
        # land on the flap-saturated watchdog_alerts topic.
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
    def test_blocked_question_lists_the_ats_options(
        self, bus, topics_config, verbosity_config,
    ):
        """application_blocked had no branch: the generic fallback printed the
        envelope as `key: value`, so a list-valued `options` reached Diego as a
        Python repr of the very labels an answer must be copied from."""
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Capital One", "title": "Director",
             "question": "Answer needed for How Did You Hear About Us?",
             "options": ["Internet", "Contacted by Recruiter"]},
        )
        body = notifier._format_payload(event)
        assert "Capital One" in body
        assert "1. Internet" in body
        assert "2. Contacted by Recruiter" in body
        assert "[" not in body  # not a repr of the list

    def test_blocked_question_does_not_hide_tail_options(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "Choose a source",
             "options": ["choice%d" % i for i in range(60)]},
        )
        body = notifier._format_payload(event)
        assert "60. choice59" in body
        assert "more (see" not in body

    def test_blocked_question_without_options_lists_no_choices(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_BLOCKED, "applier",
            {"company": "Acme", "question": "Why do you want this role?"},
        )
        body = notifier._format_payload(event)
        assert "Why do you want this role?" in body
        assert "EXACTLY" not in body

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

    def test_format_message_uses_supplied_route_verdict(self, bus, topics_config,
                                                        verbosity_config):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.AGENT_ITERATION,
            "postgres-sync",
            {"reason": "success", "counters": {"exit_code": 1}},
            priority=Priority.LOW,
        )
        route = classify(event, known_topic_keys=notifier.topics.keys())

        with patch(
            "events.formatting.format_event_message", return_value="rendered",
        ) as render:
            msg = notifier.format_message(event, route=route)

        assert msg == "rendered"
        assert render.call_args.kwargs["verdict"] is route.verdict

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
        """Routing classification remains total for raw machine mailbox
        envelopes even though handle() now keeps them bus-only. The target is
        still ``scribe_daily`` for shared policy consumers; Telegram's
        delivery-layer guard prevents the duplicate chat message. Only a
        NOTIFICATION message_type triggers the explicit override branch.
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

    def test_critical_failure_targets_alerts(self, bus, topics_config, verbosity_config):
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

    def test_high_priority_event_has_exactly_one_target(
        self, bus, topics_config, verbosity_config,
    ):
        """v3 (P2 — one event, one message): cross-posting is REMOVED.
        resolve_all_targets() must return exactly ONE target for every
        event, including the HIGH+ signals the pre-v3 CROSS_POST_TO_ALERTS
        table duplicated onto watchdog_alerts.

        JOB_HIGH_SCORE is the clean witness: pre-v3 it delivered to BOTH
        jobflow_decisions and watchdog_alerts; in v3 it is INFO class on the
        jobflow_firehose domain topic (thread 101) — and ONLY there. The
        phone-worthiness of a >=9.0 score is carried by the WhatsApp tier,
        not by a second Telegram copy.
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
        assert len(targets) == 1, (
            f"v3 removed cross-posting; expected exactly one target, got "
            f"{targets!r}"
        )
        assert targets[0][2] == "101", (
            f"job_high_score must land on jobflow_firehose (101); got "
            f"{targets[0]!r}"
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


class TestModelRateLimitedButtons:
    """Task 6 review IMPORTANT 1/2 regression guard: buttons_for() had zero
    callers before this — handle() never passed `buttons` into _deliver, so
    no MODEL_RATE_LIMITED alert could ever carry a button no matter what
    events.override_buttons computed. This pins the wiring at the
    production call site (cron.scheduler._deliver_result, the path taken
    when no send_fn is injected)."""

    def _diverted_event(self):
        return Event.create(
            EventType.MODEL_RATE_LIMITED, "matcher",
            {"provider": "deepseek", "model": "deepseek-v4-pro",
             "reason": "rate_limit", "detector": "runtime",
             "outcome": "diverted", "fallback_provider": "openai-codex",
             "fallback_model": "gpt-5.6-sol", "resets_at": "",
             "diverted_calls": 3, "episode_opened_at": "x"},
        )

    def test_diverted_event_reaches_deliver_result_with_buttons(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = self._diverted_event()

        with patch("cron.scheduler._deliver_result") as deliver_result_mock:
            notifier.handle(event)

        deliver_result_mock.assert_called_once()
        _args, kwargs = deliver_result_mock.call_args
        assert kwargs.get("buttons") is not None, (
            "a runtime-detector/diverted-outcome MODEL_RATE_LIMITED event "
            "must reach _deliver_result with a non-None buttons spec"
        )

    def test_unrelated_event_type_reaches_deliver_result_without_buttons(
        self, bus, topics_config, verbosity_config,
    ):
        """The buttons_for() call is guarded to MODEL_RATE_LIMITED only —
        an unrelated event type must never compute or forward buttons."""
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.APPLICATION_FAILED, "applier", {"error": "timeout"},
            priority=Priority.CRITICAL,
        )

        with patch("cron.scheduler._deliver_result") as deliver_result_mock:
            notifier.handle(event)

        deliver_result_mock.assert_called_once()
        _args, kwargs = deliver_result_mock.call_args
        assert kwargs.get("buttons") is None

    def test_record_call_uses_the_actual_buttons_for_token_and_payload(
        self, bus, topics_config, verbosity_config,
    ):
        """Task 7 review Important-2: the record() block at
        telegram_notifier.py:429-461 had zero test coverage. Every other
        test in this suite seeds events.override_callback_state via a
        hardcoded token through a local ``_record_target()`` helper that
        never calls buttons_for() -- so a mis-parse of
        ``buttons[0][0]["callback_data"].split(":", 2)[2]``, or the
        ``if buttons:`` guard shifting, would leave every real button
        answering "This prompt has already been resolved." in production
        with no test failing.

        This drives a REAL event through handle(), reads the token out of
        buttons_for()'s ACTUAL output (never recomputed), and asserts
        events.override_callback_state.pop() returns a target matching
        the event's own payload -- pinning both the parse and the
        no-desync property between the two.
        """
        from events import override_callback_state

        override_callback_state.reset()
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = self._diverted_event()

        try:
            with patch("cron.scheduler._deliver_result") as deliver_result_mock:
                notifier.handle(event)

            deliver_result_mock.assert_called_once()
            _args, kwargs = deliver_result_mock.call_args
            buttons = kwargs.get("buttons")
            assert buttons, "expected a non-empty buttons spec for a diverted event"

            token = buttons[0][0]["callback_data"].split(":", 2)[2]
            target = override_callback_state.pop(token)

            assert target is not None, (
                "override_callback_state has no entry for the token "
                "buttons_for() actually produced -- the record() block's "
                "token extraction has desynced from buttons_for()'s "
                "callback_data format"
            )
            assert target["provider"] == event.payload["provider"]
            assert target["model"] == event.payload["model"]
            assert target["replacement_provider"] == event.payload["fallback_provider"]
            assert target["replacement_model"] == event.payload["fallback_model"]
        finally:
            override_callback_state.reset()


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

        # v3: only TRACE routes batch — NORMAL job_scored now batches too,
        # so the immediate-delivery witness is an INFO-class event.
        # stage_transition routes to "jobflow_firehose" (v2) with mode="all".
        event = Event.create(
            EventType.STAGE_TRANSITION, "tracker",
            {"prior_stage": "discovered", "new_stage": "applied",
             "title": "Engineer", "company": "Beta"},
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

        # Buffer two low-priority events on the "jobflow_firehose" topic
        # (mode=all). NB titles must differ by LETTERS, not digits — the v3
        # RepeatGuard fingerprint collapses digit runs, so "Job 0"/"Job 1"
        # would count as verbatim repeats.
        for title in ("Alpha Analyst", "Beta Engineer"):
            event = Event.create(
                EventType.JOB_DISCOVERED, "scout",
                {"title": title, "company": "Acme", "source": "Indeed"},
                priority=Priority.LOW,
            )
            notifier.handle(event)

        assert len(sent) == 0  # still buffered

        # Force flush with max_age=0 (flush all)
        notifier._flush_stale_batches(max_age=0)

        assert len(sent) == 1
        assert "Batched (2 events)" in sent[0]

    def test_batched_brief_preserves_multiline_body(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = TelegramNotifier(
            bus,
            topics_path=topics_config,
            verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )
        brief = (
            "── Critic · daily skill review ──\n"
            "VERDICT: No changes recommended.\n"
            "\n"
            "REVIEWED: 20 skills, 7 days of evidence (28,796 audit lines)\n"
            "TOP REJECTED EVIDENCE:\n"
            "  orchestrator ×3 → DevFlow actor IDs, not skill use\n"
            "SCORES MOVED: none\n"
            "RETIREMENT FLAGS: none\n"
            "\n"
            "ACTION NEEDED: none"
        )
        notifier.handle(Event.create(
            EventType.AGENT_ITERATION,
            "critic",
            {"agent": "critic", "summary": "no changes", "brief": brief},
            priority=Priority.LOW,
        ))
        assert sent == []  # TRACE/LOW event is buffered

        notifier._flush_stale_batches(max_age=0)

        assert len(sent) == 1
        assert "Batched (1 events)" in sent[0]
        assert brief in sent[0]
        assert "VERDICT: No changes recommended.\n\nREVIEWED: 20 skills" in sent[0]
        assert "RETIREMENT FLAGS: none\n\nACTION NEEDED: none" in sent[0]

    def test_batch_flush_emits_synthetic_delivered_event(
        self, bus, topics_config, verbosity_config,
    ):
        """Routing-v3 observability gap (2026-07-20): batch flushes call
        _deliver() without event=, so hourly "Batched (N events)" sends
        left ZERO notification_delivered rows — per-topic delivery audits
        (routing_v3_24h_verify) undercount actual chat messages and can't
        verify batch cadence. Fix: one synthetic NOTIFICATION_DELIVERED
        per flush with original_event_type="batch_flush" + batch_count,
        NOT one per constituent event (Phase 1 volume scoping stands).
        """
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: None,
        )
        # Titles differ by LETTERS (RepeatGuard collapses digit runs).
        for title in ("Alpha Analyst", "Beta Engineer"):
            notifier.handle(Event.create(
                EventType.JOB_DISCOVERED, "scout",
                {"title": title, "company": "Acme", "source": "Indeed"},
                priority=Priority.LOW,
            ))
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []

        notifier._flush_stale_batches(max_age=0)

        delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
        assert len(delivered) == 1, (
            f"expected exactly one synthetic NOTIFICATION_DELIVERED per "
            f"batch flush, got {len(delivered)}"
        )
        evt = delivered[0]
        assert evt.priority == Priority.LOW
        assert evt.payload["original_event_type"] == "batch_flush"
        assert evt.payload["batch_count"] == 2
        assert evt.payload["platform"] == "telegram"
        assert evt.payload["target"]["chat_id"] == "-1001234567890"
        assert evt.payload["target"]["thread_id"] == "101"  # jobflow_firehose
        assert evt.payload["target"]["topic_key"] == "jobflow_firehose"
        assert evt.payload["latency_ms"] >= 0
        # No single originating event — the field must not point anywhere.
        assert "original_event_id" not in evt.payload

    def test_batch_flush_send_failure_emits_no_delivered_event(
        self, bus, topics_config, verbosity_config,
    ):
        """A failed flush send must NOT claim delivery — the synthetic
        event fires only on the success path, so the ledger never counts
        a batch the chat never saw."""
        calls = {"n": 0}

        def flaky_send(chat_id, thread_id, msg):
            calls["n"] += 1
            raise RuntimeError("Bad Gateway")

        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=flaky_send,
        )
        notifier.handle(Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        ))
        assert calls["n"] == 0  # buffered, send not attempted yet

        notifier._flush_stale_batches(max_age=0)

        assert calls["n"] == 1, "flush must have attempted the send"
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []

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

    def _cluster_event(self, source, timestamp, failure_type="captcha"):
        # NB: tests that need TWO deliveries must vary failure_type (a
        # LETTER difference) — the v3 RepeatGuard fingerprint collapses
        # digit runs, so differing timestamps/counts alone still count as
        # verbatim repeats within its 30-min real-time window.
        evt = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, source,
            {
                "source": source, "failure_type": failure_type, "count": 3,
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
        and the user sees two ``#watchdog_alerts`` messages back-to-back
        (v3: clusters are systemic signals and stay on the alert stream —
        they no longer demote to jobflow_firehose).
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

        firehose_deliveries = [s for s in sent if s[0] == "100"]
        assert len(firehose_deliveries) == 1, (
            f"second cluster for same source in same 30-min bucket must "
            f"be suppressed; got {len(firehose_deliveries)} deliveries: "
            f"{sent!r}"
        )

    def test_cluster_in_next_bucket_re_delivers(
        self, bus, topics_config, verbosity_config, monkeypatch,
    ):
        """A cluster for the same agent in the NEXT 30-min bucket must
        deliver again — the dedup is rate-limit, not permanent
        suppression. Without this, an Applier failure that recurs the
        next morning would silently never re-alert.

        Advances a fake monotonic clock past the RepeatGuard's 30-min
        window between the two events — in production the next-bucket
        event really does arrive 30+ minutes later."""
        sent = []
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append((thread_id, msg)),
        )
        clock = {"now": 1000.0}
        import events.noise_guards as ng
        monkeypatch.setattr(ng.time, "monotonic", lambda: clock["now"])
        evt1 = self._cluster_event("applier", "2026-04-29T10:00:00+00:00")
        evt2 = self._cluster_event("applier", "2026-04-29T10:31:00+00:00")

        notifier.handle(evt1)
        clock["now"] += 31 * 60
        notifier.handle(evt2)

        firehose_deliveries = [s for s in sent if s[0] == "100"]
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

        firehose_deliveries = [s for s in sent if s[0] == "100"]
        assert len(firehose_deliveries) == 2, (
            f"different sources must both deliver; got {sent!r}"
        )

    def test_dedup_does_not_affect_other_event_types(
        self, bus, topics_config, verbosity_config,
    ):
        """LRU dedup is scoped to AGENT_FAILURE_CLUSTER only. A CRON_FAILED
        event from the same source in the same window MUST still deliver —
        the dedup must not bleed across event types.

        v3 topology note: the cluster (systemic) stays on watchdog_alerts
        (100), while the single cron_failed from a JobFlow pipeline agent
        demotes to jobflow_firehose (101) — both must deliver."""
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

        alert_deliveries = [s for s in sent if s[0] == "100"]
        firehose_deliveries = [s for s in sent if s[0] == "101"]
        assert len(alert_deliveries) == 1 and len(firehose_deliveries) == 1, (
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
        # The RepeatGuard would shadow the LRU behaviour under test (its
        # 30-min real-time window sees an identical body), so clear it:
        # this test pins the cluster LRU, not the repeat guard.
        notifier._repeat_guard._seen.clear()
        replay = self._cluster_event("applier", "2026-04-29T10:14:00+00:00")
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

        alert_deliveries = [s for s in sent if s[0] == "100"]
        assert len(alert_deliveries) == 1, (
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
        alert_deliveries = [s for s in sent if s[0] == "100"]
        assert len(alert_deliveries) == 1


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
        """The routing policy must place WATCHDOG_DAILY in watchdog_alerts.

        Pin the routing alongside the other watchdog signals so a future
        cutover that splits watchdog topics has to update one obvious place.
        """
        assert classify(self._daily_event()).topic_key == "watchdog_alerts"

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
    ev = Event.create(
        EventType.WATCHDOG_BURST, "watchdog",
        {"count": 1, "trigger": "burst_threshold", "transitions": []},
    )
    assert classify(ev).topic_key == "watchdog_alerts"


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


class TestBatchAgeSurvivesRestart:
    """Restart-churn starvation fix (confirmed live 2026-07-21): batch age
    must be measured from a persisted wall-clock first-buffered timestamp,
    not re-stamped with the new process's time.monotonic() on every
    __init__. Under ~20-30 min gateway lifetimes the old behavior meant a
    3600s batch window NEVER elapsed — messages buffered at 18:07Z were
    still undelivered at 00:44Z."""

    def _write_configs(self, tmp_path):
        (tmp_path / "telegram").mkdir(exist_ok=True)
        (tmp_path / "telegram" / "topics.json").write_text(
            '{"group_chat_id": "-1", "topics": {"system": {"thread_id": 15}}}')
        (tmp_path / "telegram" / "verbosity.json").write_text(
            '{"system": {"mode": "all"}}')

    def _write_batch_state(self, tmp_path, state):
        path = tmp_path / "notifications" / "notifier_batch.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def test_restored_batch_with_old_started_at_flushes_immediately(
        self, tmp_path, monkeypatch,
    ):
        """A buffer restored with a 2h-old persisted started_at is already
        past the 3600s window: the FIRST _flush_stale_batches call must
        flush it, without the new process having to survive another hour."""
        from datetime import datetime, timedelta, timezone

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_configs(tmp_path)
        two_hours_ago = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        self._write_batch_state(tmp_path, {
            "buffer": {"-1:15": ["starved msg 1", "starved msg 2"]},
            "started_at": {"-1:15": two_hours_ago},
        })

        bus = EventBus(db_path=tmp_path / "db.sqlite")
        try:
            sent = []
            notifier = TelegramNotifier(
                bus, send_fn=lambda chat_id, thread_id, msg: sent.append(msg))
            notifier._flush_stale_batches()  # default max_age=3600s

            assert len(sent) == 1, (
                f"restored 2h-old batch must flush on first stale sweep; "
                f"sent={sent!r}"
            )
            assert "Batched (2 events)" in sent[0]
            # The synthetic batch_flush ledger row (18891230c) must survive
            # the restore path too.
            delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
            assert len(delivered) == 1
            assert delivered[0].payload["original_event_type"] == "batch_flush"
            assert delivered[0].payload["batch_count"] == 2
        finally:
            bus.close()

    def test_restored_batch_with_fresh_started_at_does_not_flush_early(
        self, tmp_path, monkeypatch,
    ):
        """A just-persisted batch restored after a quick restart keeps its
        remaining age — it must NOT flush before the window elapses."""
        from datetime import datetime, timezone

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_configs(tmp_path)
        self._write_batch_state(tmp_path, {
            "buffer": {"-1:15": ["fresh msg"]},
            "started_at": {"-1:15": datetime.now(timezone.utc).isoformat()},
        })

        bus = EventBus(db_path=tmp_path / "db.sqlite")
        try:
            sent = []
            notifier = TelegramNotifier(
                bus, send_fn=lambda chat_id, thread_id, msg: sent.append(msg))
            notifier._flush_stale_batches()
            assert sent == [], "fresh restored batch must wait out the window"
            assert notifier._batch_buffer.get("-1:15") == ["fresh msg"]
        finally:
            bus.close()

    def test_legacy_buffer_only_state_loads_and_seeds_now(
        self, tmp_path, monkeypatch,
    ):
        """Backward compat: a pre-fix state file ({"buffer": {...}} with no
        started_at) still restores; with no persisted age the key seeds at
        now (old behavior — no crash, no premature flush)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        self._write_configs(tmp_path)
        self._write_batch_state(tmp_path, {
            "buffer": {"-1:15": ["legacy msg"]},
        })

        bus = EventBus(db_path=tmp_path / "db.sqlite")
        try:
            sent = []
            notifier = TelegramNotifier(
                bus, send_fn=lambda chat_id, thread_id, msg: sent.append(msg))
            assert notifier._batch_buffer.get("-1:15") == ["legacy msg"]
            notifier._flush_stale_batches()
            assert sent == []
        finally:
            bus.close()

    def test_persist_writes_wall_clock_started_at(
        self, bus, topics_config, verbosity_config, tmp_path, monkeypatch,
    ):
        """Buffering a message must persist a parseable wall-clock
        started_at for the key alongside the buffer, and flushing must
        remove both."""
        from datetime import datetime, timezone

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda *a, **k: None,
        )
        # JOB_DISCOVERED at LOW is TRACE → batches (same witness as the
        # TestLowPriorityBatching cases).
        notifier.handle(Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Analyst", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        ))
        assert notifier._batch_buffer, "event must have batched"
        state_path = tmp_path / "notifications" / "notifier_batch.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert set(state["started_at"]) == set(state["buffer"])
        for iso in state["started_at"].values():
            parsed = datetime.fromisoformat(iso)
            assert parsed.tzinfo is not None, (
                "started_at must be timezone-aware wall clock")
            age = (datetime.now(timezone.utc) - parsed).total_seconds()
            assert 0 <= age < 60

        notifier._flush_stale_batches(max_age=0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["buffer"] == {}
        assert state["started_at"] == {}


class TestBatchFlushFailureRequeue:
    """Lossy batch-flush gap (confirmed live 2026-07-20/21): _flush_batch_key
    popped the key's messages BEFORE delivering, and _deliver() swallows send
    exceptions — so while Telegram sat in a persistent httpx.ReadError
    reconnect loop, the starved batch (buffered since 18:07Z) simply
    vanished: no requeue, no NOTIFICATION_FAILED, no trace beyond the
    generic delivery error line. The ledger contract stands (a batch the
    chat never saw must NOT produce NOTIFICATION_DELIVERED); these tests
    pin the recovery: a failed flush restores the popped messages + the
    key's wall-clock age, emits ONE synthetic NOTIFICATION_FAILED
    (original_event_type="batch_flush", mirroring 18891230c's delivered
    twin), persists the restored state, and arms a per-key backoff so a
    dead transport isn't hammered once per handled event.
    """

    KEY = "-1001234567890:101"  # jobflow_firehose in the topics_config fixture

    def _buffer_two(self, notifier):
        # Titles differ by LETTERS (RepeatGuard collapses digit runs).
        for title in ("Alpha Analyst", "Beta Engineer"):
            notifier.handle(Event.create(
                EventType.JOB_DISCOVERED, "scout",
                {"title": title, "company": "Acme", "source": "Indeed"},
                priority=Priority.LOW,
            ))

    def _failing_notifier(self, bus, topics_config, verbosity_config):
        calls = {"n": 0}

        def failing_send(chat_id, thread_id, msg):
            calls["n"] += 1
            raise RuntimeError("Bad Gateway")

        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=failing_send,
        )
        return notifier, calls

    def test_failed_flush_restores_buffer_and_started_at(
        self, bus, topics_config, verbosity_config, tmp_path, monkeypatch,
    ):
        """The popped messages go back to the buffer (front, in order) with
        the key's original wall-clock started_at, and the restored state is
        persisted — so neither an in-process retry nor a restart treats the
        starved batch as fresh (or, worse, gone)."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        notifier, calls = self._failing_notifier(
            bus, topics_config, verbosity_config)
        self._buffer_two(notifier)
        messages_before = list(notifier._batch_buffer[self.KEY])
        started_before = notifier._batch_started_at[self.KEY]

        notifier._flush_stale_batches(max_age=0)

        assert calls["n"] == 1, "flush must have attempted the send"
        assert notifier._batch_buffer.get(self.KEY) == messages_before, (
            "failed flush must restore the popped messages in order"
        )
        assert notifier._batch_started_at.get(self.KEY) == started_before, (
            "failed flush must preserve the key's wall-clock started_at"
        )
        assert self.KEY in notifier._batch_timestamps
        state = json.loads(
            (tmp_path / "notifications" / "notifier_batch.json")
            .read_text(encoding="utf-8"))
        assert state["buffer"][self.KEY] == messages_before
        assert state["started_at"][self.KEY] == started_before

    def test_failed_flush_emits_synthetic_notification_failed(
        self, bus, topics_config, verbosity_config,
    ):
        """One NOTIFICATION_FAILED per failed flush attempt — the failure
        twin of 18891230c's batch_flush NOTIFICATION_DELIVERED — so the
        ledger shows the outage instead of silence. No DELIVERED row."""
        notifier, _calls = self._failing_notifier(
            bus, topics_config, verbosity_config)
        self._buffer_two(notifier)

        notifier._flush_stale_batches(max_age=0)

        failed = bus.query(event_type=EventType.NOTIFICATION_FAILED)
        assert len(failed) == 1, (
            f"expected exactly one synthetic NOTIFICATION_FAILED per failed "
            f"batch flush, got {len(failed)}"
        )
        evt = failed[0]
        assert evt.priority == Priority.NORMAL
        assert evt.payload["original_event_type"] == "batch_flush"
        assert evt.payload["batch_count"] == 2
        assert evt.payload["platform"] == "telegram"
        assert evt.payload["target"]["chat_id"] == "-1001234567890"
        assert evt.payload["target"]["thread_id"] == "101"
        assert evt.payload["target"]["topic_key"] == "jobflow_firehose"
        assert evt.payload["error"]["kind"] == "RuntimeError"
        assert "Bad Gateway" in evt.payload["error"]["message"]
        # No single originating event — the field must not point anywhere.
        assert "original_event_id" not in evt.payload
        assert bus.query(event_type=EventType.NOTIFICATION_DELIVERED) == []

    def test_retry_backoff_prevents_immediate_resend(
        self, bus, topics_config, verbosity_config, monkeypatch,
    ):
        """_flush_stale_batches runs on EVERY handled event; without a
        per-key backoff a restored stale batch would hammer a dead
        Telegram once per event. After a failure the key must not retry
        until the backoff window has elapsed."""
        import events.subscribers.telegram_notifier as tn

        clock = {"now": 1000.0}
        monkeypatch.setattr(tn.time, "monotonic", lambda: clock["now"])
        notifier, calls = self._failing_notifier(
            bus, topics_config, verbosity_config)
        self._buffer_two(notifier)

        notifier._flush_stale_batches(max_age=0)
        assert calls["n"] == 1

        notifier._flush_stale_batches(max_age=0)  # immediate re-sweep
        assert calls["n"] == 1, (
            "a failed key must NOT re-send before the backoff elapses"
        )
        assert notifier._batch_buffer.get(self.KEY), (
            "backoff skip must leave the buffer intact"
        )

        clock["now"] += tn.BATCH_RETRY_BACKOFF_SECONDS + 1
        notifier._flush_stale_batches(max_age=0)
        assert calls["n"] == 2, "after the backoff the key must retry"

    def test_retry_success_delivers_full_batch_and_clears_backoff(
        self, bus, topics_config, verbosity_config, monkeypatch,
    ):
        """Once the transport recovers, the retried flush delivers the
        restored messages, emits the normal batch_flush
        NOTIFICATION_DELIVERED, empties the buffer, and clears the
        backoff so later flushes are immediate again."""
        import events.subscribers.telegram_notifier as tn

        clock = {"now": 1000.0}
        monkeypatch.setattr(tn.time, "monotonic", lambda: clock["now"])
        sent = []
        state = {"fail": True}

        def flaky_send(chat_id, thread_id, msg):
            if state["fail"]:
                raise RuntimeError("Bad Gateway")
            sent.append(msg)

        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=flaky_send,
        )
        self._buffer_two(notifier)
        notifier._flush_stale_batches(max_age=0)
        assert sent == []

        state["fail"] = False
        clock["now"] += tn.BATCH_RETRY_BACKOFF_SECONDS + 1
        notifier._flush_stale_batches(max_age=0)

        assert len(sent) == 1
        assert "Batched (2 events)" in sent[0]
        assert self.KEY not in notifier._batch_buffer
        delivered = bus.query(event_type=EventType.NOTIFICATION_DELIVERED)
        assert len(delivered) == 1
        assert delivered[0].payload["original_event_type"] == "batch_flush"
        assert delivered[0].payload["batch_count"] == 2

        # Backoff cleared: a fresh batch flushes without waiting.
        notifier.handle(Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Gamma Developer", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        ))
        notifier._flush_stale_batches(max_age=0)
        assert len(sent) == 2, "success must clear the per-key backoff"

    def test_failed_flush_restore_caps_at_batch_max(
        self, bus, topics_config, verbosity_config, caplog,
    ):
        """A restored buffer respects BATCH_MAX_MESSAGES: oldest messages
        drop (with a log line) so a dead transport can't grow the buffer
        unbounded across repeated failed flushes."""
        import events.subscribers.telegram_notifier as tn

        notifier, _calls = self._failing_notifier(
            bus, topics_config, verbosity_config)
        msgs = [f"msg {chr(ord('A') + i)}" for i in range(25)]
        notifier._batch_buffer[self.KEY] = list(msgs)
        notifier._batch_timestamps[self.KEY] = 0.0
        notifier._batch_started_at[self.KEY] = "2026-07-20T18:07:00+00:00"

        with caplog.at_level("WARNING"):
            notifier._flush_stale_batches(max_age=0)

        restored = notifier._batch_buffer.get(self.KEY)
        assert restored == msgs[-tn.BATCH_MAX_MESSAGES:], (
            "restore must cap oldest-out at BATCH_MAX_MESSAGES"
        )
        assert any("dropped" in r.message for r in caplog.records), (
            "capping must leave a log trace of the dropped messages"
        )

    def test_buffer_stays_capped_while_backoff_blocks_size_flush(
        self, bus, topics_config, verbosity_config, monkeypatch,
    ):
        """During the backoff window handle() keeps appending and the
        size-triggered flush is skipped — the buffer must still stay at
        BATCH_MAX_MESSAGES (oldest-out), keeping the newest message."""
        import events.subscribers.telegram_notifier as tn

        clock = {"now": 1000.0}
        monkeypatch.setattr(tn.time, "monotonic", lambda: clock["now"])
        notifier, calls = self._failing_notifier(
            bus, topics_config, verbosity_config)
        msgs = [f"msg {chr(ord('A') + i)}" for i in range(tn.BATCH_MAX_MESSAGES)]
        notifier._batch_buffer[self.KEY] = list(msgs)
        notifier._batch_timestamps[self.KEY] = clock["now"]
        notifier._batch_started_at[self.KEY] = "2026-07-20T18:07:00+00:00"

        notifier._flush_stale_batches(max_age=0)  # fails, arms backoff
        assert calls["n"] == 1

        notifier.handle(Event.create(
            EventType.JOB_DISCOVERED, "scout",
            {"title": "Gamma Developer", "company": "Acme", "source": "Indeed"},
            priority=Priority.LOW,
        ))

        assert calls["n"] == 1, "size-triggered flush must respect the backoff"
        buf = notifier._batch_buffer.get(self.KEY)
        assert len(buf) == tn.BATCH_MAX_MESSAGES, (
            f"buffer must stay capped during backoff; got {len(buf)}"
        )
        assert "Gamma Developer" in buf[-1], (
            "cap must drop the OLDEST messages, keeping the newest"
        )


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
        # v3: NORMAL job_scored is TRACE and batches — use an INFO-class
        # event (application_submitted) as the immediate-delivery witness.
        original = Event.create(
            EventType.APPLICATION_SUBMITTED, "applier",
            {"title": "Engineer", "company": "Beta"},
            priority=Priority.NORMAL,
        )
        original_id = bus.emit(
            event_type=EventType.APPLICATION_SUBMITTED, source="applier",
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
        assert evt.payload["original_event_type"] == "application_submitted"
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
            event_type=EventType.APPLICATION_SUBMITTED, source="applier",
            payload={"title": "Engineer", "company": "Beta"},
            priority=Priority.NORMAL,
        )
        # Recreate the Event object as handle() would receive it
        original = bus.query(event_type=EventType.APPLICATION_SUBMITTED)[0]

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
        bus.emit(
            event_type=EventType.APPLICATION_SUBMITTED, source="applier",
            payload={"title": "X", "company": "Y"},
            priority=Priority.NORMAL,
        )
        original = bus.query(event_type=EventType.APPLICATION_SUBMITTED)[0]

        # Break only the reverse-signal emit by replacing bus.emit with a
        # raising version AFTER the original event was emitted by the
        # producer above. The notifier's _deliver() will call bus.emit and
        # the wrapper must swallow.
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

    def test_act_event_emits_exactly_one_delivered_event(
        self, bus, topics_config, verbosity_config,
    ):
        """v3 (P2): cross-posting is gone — interview_signal delivers ONE
        message to the action_required topic (alias-resolved onto
        jobflow_decisions, thread 102, in this pre-cutover fixture) and
        emits exactly ONE NOTIFICATION_DELIVERED, so the audit log answers
        "did Telegram show this?" without double-counting.
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
        assert len(delivered) == 1, (
            f"expected exactly 1 NOTIFICATION_DELIVERED (one event, one "
            f"message); got {len(delivered)}"
        )
        d = delivered[0]
        assert d.payload["target"]["thread_id"] == "102", (
            f"action_required must alias-resolve onto jobflow_decisions "
            f"(102); got {d.payload['target']!r}"
        )
        assert d.payload["target"]["topic_key"] == "jobflow_decisions"
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


class TestDailyBriefTransportSuppression:
    """Raw machine transport and digest telemetry stay on the bus without
    cluttering Daily Brief; real Scribe and user narratives still deliver."""

    def _notifier(self, bus, topics_config, verbosity_config, sent):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda *a, **k: sent.append(a),
        )

    def test_digest_generated_is_bus_only(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = self._notifier(bus, topics_config, verbosity_config, sent)
        bus.emit(
            event_type=EventType.DIGEST_GENERATED,
            source="scribe",
            payload={"mode": "pm", "body_length": 1200},
        )
        event = bus.query(event_type=EventType.DIGEST_GENERATED)[0]

        notifier.handle(event)

        assert sent == []
        assert all(not msgs for msgs in notifier._batch_buffer.values()), \
            "digest telemetry must not even be batched"
        assert bus.query(event_type=EventType.DIGEST_GENERATED) == [event], \
            "bus-only means retained on the bus, not discarded"

    def test_machine_mailbox_message_is_bus_only(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = self._notifier(bus, topics_config, verbosity_config, sent)
        event = Event.create(
            EventType.MAILBOX_MESSAGE,
            "matcher",
            {
                "message_type": "SCORE_RESULT",
                "from": "matcher",
                "to": "main",
                "summary": "score 9.0 for Acme",
            },
        )

        notifier.handle(event)

        assert sent == []
        assert all(not msgs for msgs in notifier._batch_buffer.values()), \
            "machine mailbox transport must not even be batched"

    def test_notification_mailbox_message_still_delivers(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = self._notifier(bus, topics_config, verbosity_config, sent)
        event = Event.create(
            EventType.MAILBOX_MESSAGE,
            "scribe",
            {
                "message_type": "NOTIFICATION",
                "from": "scribe",
                "to": "main",
                "summary": "Daily narrative",
            },
        )

        notifier.handle(event)

        assert len(sent) == 1
        assert sent[0][1] == "105"
        assert "Daily narrative" in sent[0][2]

    def test_user_inbound_message_still_delivers(
        self, bus, topics_config, verbosity_config,
    ):
        sent = []
        notifier = self._notifier(bus, topics_config, verbosity_config, sent)
        event = Event.create(
            EventType.USER_INBOUND_MESSAGE,
            "telegram",
            {"text": "Please review today's brief"},
        )

        notifier.handle(event)

        assert len(sent) == 1
        assert sent[0][1] == "105"


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

    def test_failure_cluster_for_jobflow_agent_stays_on_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        # v3 deliberate change vs the 2026-07-16 blanket demotion: a
        # CLUSTER (>=3 failures) is systemic and page-worthy even from a
        # pipeline agent — it stays on the alerts topic. Only SINGLE
        # failures demote to the jobflow domain topic.
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.AGENT_FAILURE_CLUSTER, "applier",
            {"source": "applier", "count": 3},
        )
        assert notifier.resolve_target(ev)[2] == "100"

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
        # v3: single failures (cron_failed, cron_stale) demote to the
        # jobflow domain topic; CONSECUTIVE failures are systemic and stay
        # on alerts (tested below).
        notifier = self._notifier(bus, topics_config, verbosity_config)
        for et in (EventType.CRON_FAILED, EventType.CRON_STALE):
            for job in ("jobflow-tracker-cycle", "jobflow-applier",
                        "sentinel-vip-evening"):
                ev = Event.create(et, job, {"job_name": job})
                assert notifier.resolve_target(ev)[2] == "101", \
                    f"{et.type_string} from {job} should route to jobflow_firehose"

    def test_jobflow_consecutive_cron_failures_stay_on_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        ev = Event.create(
            EventType.CRON_FAILED_CONSECUTIVE, "jobflow-tracker-cycle",
            {"job_name": "jobflow-tracker-cycle"},
        )
        assert notifier.resolve_target(ev)[2] == "100"

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


class TestBootSummaryBody:
    """BOOT_SUMMARY must reach the plain-language body, not the generic
    key:value fallback — `failures` and `anomalies` are lists, and the
    fallback renders lists as Python reprs (2026-07-27)."""

    def test_body_is_plain_language_not_a_list_repr(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.BOOT_SUMMARY, "laptop-start",
            {"boot_id": "20260727-132212", "state": "failed",
             "total": 22, "done": 20, "failed": 2, "skipped": 1,
             "failures": ["[critical] gbrain-http: port 7483 never opened"],
             "anomalies": ["task-329-kill: soak task killed at 578s"]},
        )
        body = notifier._format_payload(event)
        assert "gbrain-http: port 7483 never opened" in body
        assert "['" not in body and "']" not in body

    def test_boot_summary_lands_on_watchdog_alerts(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )
        event = Event.create(
            EventType.BOOT_SUMMARY, "laptop-start", {"state": "failed"},
        )
        assert notifier.resolve_target(event)[2] == "100"


class TestResourcePressureSeverityBands:
    """2026-08-14: the producer keeps sampling a live episode every 900s for the
    bus, but only a CHANGE is a message."""

    def _event(self, free_gb=2.4, band="imminent", edge=3, change="band_change"):
        return Event.create(
            EventType.RESOURCE_PRESSURE, "system",
            {
                "reasons": ["disk_low"],
                "commit_used_gb": 83.32, "commit_limit_gb": 127.2,
                "commit_pct": 65.5, "phys_used_pct": 75.8,
                "phys_available_gb": 15.3, "pagefile_allocated_gb": 64.0,
                "pagefile_growth_gb_10min": 0.0,
                "disk_c_free_gb": free_gb, "disk_band": band,
                "disk_band_edge_gb": edge, "change": change,
                "thresholds": {"disk_free_gb": 45.0},
            },
        )

    def _notifier(self, bus, topics_config, verbosity_config, sent):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
            send_fn=lambda chat_id, thread_id, msg: sent.append(msg),
        )

    def test_a_band_change_is_delivered(self, bus, topics_config, verbosity_config):
        sent = []
        self._notifier(bus, topics_config, verbosity_config, sent).handle(
            self._event(change="band_change"))
        assert len(sent) == 1
        assert "IMMINENT" in sent[0].upper()

    def test_a_sustained_repeat_is_bus_only(self, bus, topics_config, verbosity_config):
        sent = []
        self._notifier(bus, topics_config, verbosity_config, sent).handle(
            self._event(change="sustained_repeat"))
        assert sent == []

    def test_a_deepening_band_defeats_the_repeat_guard(
        self, bus, topics_config, verbosity_config,
    ):
        """The end-to-end point: consecutive events inside the 30-min window
        used to collapse into one message no matter how bad the disk got."""
        sent = []
        notifier = self._notifier(bus, topics_config, verbosity_config, sent)
        notifier.handle(self._event(free_gb=10.0, band="severe", edge=12))
        notifier.handle(self._event(free_gb=8.0, band="severe", edge=12,
                                    change="sustained_repeat"))
        notifier.handle(self._event(free_gb=2.4, band="imminent", edge=3))
        assert len(sent) == 2
        assert "SEVERE" in sent[0].upper()
        assert "IMMINENT" in sent[1].upper()


class TestCronStaleBody:
    """CRON_STALE had no `_format_payload` branch, so all four of its scopes
    reached Telegram through the generic `f"{k}: {v}"` fallback — the same
    defect SR-408 fixed for SECRET_DETECTED. The wedge alert (something is
    stuck NOW) and a restart casualty (informational) were near-identical,
    and two correlation UUIDs rode along in every shutdown attribution.
    """

    def _notifier(self, bus, topics_config, verbosity_config):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )

    def test_gateway_stopped_is_routed_to_the_cron_stale_body(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.CRON_STALE, "cron-stale-monitor",
            {
                "job_id": "9823bee8f270", "job_name": "postgres-sync",
                "scope": "gateway_stopped", "exit_reason": "graceful",
                "age_seconds": 43,
                "gateway_stopped_event_id": "d5854c62-53be-41d7-99a9-0e9e7a9b15dd",
                "cron_started_event_id": "558ceef6-f710-4d14-8a07-537cdc06a5d5",
            },
        )
        body = notifier._format_payload(event)
        assert "cut short by a gateway shutdown (graceful) 43s" in body
        # The generic fallback's fingerprints must be gone.
        assert "scope: gateway_stopped" not in body
        assert "d5854c62" not in body

    def test_owner_exited_reads_differently_from_gateway_stopped(
        self, bus, topics_config, verbosity_config,
    ):
        """The asymmetry is the point: the shutdown attribution knows what
        killed the run and how far in; the ledger backstop knows only that
        the owner died. Flattening them would erase the distinction the two
        successor paths exist to draw."""
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.CRON_STALE, "cron-recovery",
            {
                "job_id": "1c34e737bb39", "job_name": "jobflow-scout",
                "scope": "owner_exited", "execution_id": "e-991",
                "ran_at": "2026-08-17T13:00:05-04:00",
            },
        )
        body = notifier._format_payload(event)
        assert "owner exited before recording an outcome" in body
        assert "unknown" in body
        assert "gateway shutdown" not in body
        assert "into the run" not in body
        assert "e-991" not in body

    def test_the_wedge_alert_still_reads_as_a_stuck_job(
        self, bus, topics_config, verbosity_config,
    ):
        """The scope-less original must not regress into a restart excuse —
        it is the only one of the four that means "stuck right now"."""
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.CRON_STALE, "cron-stale-monitor",
            {"job_id": "9a68c6219ff3", "job_name": "jobflow-tracker-weekly",
             "age_seconds": 1213, "threshold_seconds": 1200},
        )
        body = notifier._format_payload(event)
        assert "jobflow-tracker-weekly has been running 20m 13s" in body
        assert "shutdown" not in body and "owner exited" not in body


class TestAgentNoteDelivery:
    """End-to-end: an agent note reaches the topic with its words intact.

    The regression under test (2026-08-19): with no type that renders free
    text, callers picked boot_summary, whose body function ignores the
    payload and emits the same string every time. RepeatGuard fingerprints
    the RENDERED message, so the second of two distinct verdicts was
    suppressed with no dead-letter and no audit line — only a missing
    notification_delivered entry.
    """

    def _notifier(self, bus, topics_config, verbosity_config):
        return TelegramNotifier(
            bus, topics_path=topics_config, verbosity_path=verbosity_config,
        )

    def _note(self, headline, detail=None, attention=None, priority=None):
        payload = {"headline": headline}
        if detail is not None:
            payload["detail"] = detail
        if attention is not None:
            payload["attention"] = attention
        return Event.create(
            EventType.AGENT_NOTE, "claude-code", payload, priority=priority,
        )

    def test_headline_and_detail_survive_into_the_message(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = self._note(
            "Verdict: gbrain :7483 /health is not evidence of health",
            "It races SELECT 1 against a 3s deadline.\n"
            "A serviced tools/call is the only proof.",
        )
        message = notifier.format_message(event)
        assert "Verdict: gbrain :7483 /health is not evidence of health" in message
        assert "It races SELECT 1 against a 3s deadline." in message
        assert "A serviced tools/call is the only proof." in message
        assert "AGENT_NOTE" in message          # header survives
        assert "claude-code" in message         # source header survives

    def test_two_distinct_notes_both_deliver(
        self, bus, topics_config, verbosity_config,
    ):
        """The exact incident. Under boot_summary both rendered identically
        and the second was swallowed by RepeatGuard."""
        notifier = self._notifier(bus, topics_config, verbosity_config)
        first = notifier.format_message(
            self._note("Verdict A: the health probe is a false alarm",
                       "Re-probe with a real tools/call."))
        second = notifier.format_message(
            self._note("Verdict B: the venv trampoline is not an orphan",
                       "Check ParentProcessId before killing."))
        assert first != second
        thread = "108"
        assert notifier._repeat_guard.is_repeat(thread, first) is False
        assert notifier._repeat_guard.is_repeat(thread, second) is False

    def test_a_verbatim_repeat_is_still_suppressed(
        self, bus, topics_config, verbosity_config,
    ):
        """The guard stays armed — a looping agent costs one message per
        window, not one per fire."""
        notifier = self._notifier(bus, topics_config, verbosity_config)
        message = notifier.format_message(
            self._note("Sweep clean", "nothing to report"))
        thread = "108"
        assert notifier._repeat_guard.is_repeat(thread, message) is False
        assert notifier._repeat_guard.is_repeat(thread, message) is True

    def test_multiline_detail_is_not_collapsed(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        body = notifier._format_payload(
            self._note("H", "one\ntwo\nthree"))
        assert body == "H\none\ntwo\nthree"

    def test_note_without_headline_still_shows_its_payload(
        self, bus, topics_config, verbosity_config,
    ):
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = Event.create(
            EventType.AGENT_NOTE, "claude-code",
            {"finding": "the counter was never incremented"},
        )
        body = notifier._format_payload(event)
        assert "the counter was never incremented" in body

    def test_warn_note_passes_the_alerts_verbosity_gate(
        self, bus, topics_config, verbosity_config,
    ):
        """watchdog_alerts runs significant_only/min_priority normal in
        production; the WARN class floor is what clears it."""
        notifier = self._notifier(bus, topics_config, verbosity_config)
        event = self._note("something looks wrong", attention="warn",
                           priority=Priority.LOW)
        route = classify(event, known_topic_keys=notifier.topics.keys())
        assert route.priority.level >= Priority.NORMAL.level
        assert notifier._passes_verbosity("watchdog_alerts", route, event)
