"""Invariant + behavior tests for events.routing_policy (v3).

The invariant tests iterate the FULL EventType enum — adding a new event
type without a policy entry, or wiring an ACT route that doesn't page,
fails here instead of silently misrouting in production.
"""

import pytest

from events.outcomes import OutcomeState
from events.routing_policy import (
    ACTION_REQUIRED,
    AGENTS_MEMORY,
    ALERTS,
    Attention,
    CRITIC,
    DAILY_BRIEF,
    DEVFLOW,
    JOBFLOW,
    OPS_TRACE,
    SECURITY,
    TOPIC_ALIASES,
    WA_IMMEDIATE,
    WA_IMPORTANT,
    WA_URGENT,
    _POLICY,
    classify,
    cron_output_is_actionable,
    cron_output_is_alert,
    resolve_topic_thread,
)
from events.schema import Event, EventType, Priority


def make_event(event_type, payload=None, priority=None, source="test"):
    return Event.create(
        event_type=event_type,
        source=source,
        payload=payload or {},
        priority=priority,
    )


# ---------------------------------------------------------------- invariants

def test_every_event_type_has_policy_entry():
    missing = [et.type_string for et in EventType if et not in _POLICY]
    assert missing == [], f"EventTypes without a routing policy: {missing}"


@pytest.mark.parametrize("et", list(EventType))
def test_classify_is_total_and_sane(et):
    route = classify(make_event(et))
    assert route.topic_key, f"{et.type_string} resolved to empty topic"
    assert route.attention in Attention


@pytest.mark.parametrize("et", list(EventType))
def test_act_always_pages_and_lands_in_action_required(et):
    route = classify(make_event(et))
    if route.attention is Attention.ACT:
        assert route.topic_key == ACTION_REQUIRED
        assert route.wa_tier in (WA_IMMEDIATE, WA_URGENT)
        assert route.priority.level >= Priority.HIGH.level


@pytest.mark.parametrize("et", list(EventType))
def test_warn_clamps_to_normal_or_higher(et):
    route = classify(make_event(et))
    if route.attention is Attention.WARN:
        assert route.priority.level >= Priority.NORMAL.level


@pytest.mark.parametrize("et", list(EventType))
def test_only_trace_batches(et):
    route = classify(make_event(et))
    if route.batch:
        assert route.attention is Attention.TRACE


@pytest.mark.parametrize("et", list(EventType))
def test_info_trace_never_page_without_explicit_flag(et):
    """INFO/TRACE escalate only via explicit pins (job_high_score hook is
    covered separately); the derived predicate must not leak."""
    route = classify(make_event(et))
    if route.attention in (Attention.INFO, Attention.TRACE):
        assert route.wa_tier in (None, WA_IMPORTANT)


# ------------------------------------------------------------ ACT specifics

def test_interview_signal_is_immediate():
    route = classify(make_event(EventType.INTERVIEW_SIGNAL))
    assert route.attention is Attention.ACT
    assert route.wa_tier == WA_IMMEDIATE  # CRITICAL default


def test_secret_detected_floors_to_critical_immediate():
    route = classify(make_event(EventType.SECRET_DETECTED))
    assert route.priority is Priority.CRITICAL
    assert route.wa_tier == WA_IMMEDIATE


def test_approval_request_is_urgent_not_immediate():
    route = classify(make_event(EventType.APPROVAL_REQUEST))
    assert route.wa_tier == WA_URGENT  # HIGH → queued during quiet hours


def test_apply_packet_clamped_to_high():
    # default priority NORMAL, ACT clamps to HIGH
    route = classify(make_event(EventType.APPLY_PACKET))
    assert route.priority is Priority.HIGH


def test_route_carries_the_single_computed_verdict():
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {"agent": "postgres-sync", "counters": {"exit_code": 1}},
    ))

    assert route.verdict.state is OutcomeState.FAILED
    assert any(
        item.path == "payload.counters.exit_code" for item in route.verdict.evidence
    )


def test_failed_agent_iteration_promotes_to_warn_alerts():
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {
            "agent": "tracker",
            "reason": "success",
            "counters": {"exit_code": 1},
        },
    ))

    assert route.verdict.state is OutcomeState.FAILED
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS
    assert route.priority is Priority.HIGH
    assert route.batch is False


def test_degraded_trace_wrapper_promotes_to_warn_alerts():
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {"agent": "scout", "result": "partial"},
    ))

    assert route.verdict.state is OutcomeState.DEGRADED
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS


def test_critical_failure_without_human_gate_is_not_act():
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {"agent": "tracker", "status": "failed"},
        priority=Priority.CRITICAL,
    ))

    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS
    assert route.wa_tier == WA_URGENT


def test_failed_intrinsic_action_remains_actionable():
    route = classify(make_event(
        EventType.APPROVAL_REQUEST,
        {"status": "failed", "action_required": True, "action_kind": "approval"},
    ))

    assert route.verdict.state is OutcomeState.FAILED
    assert route.attention is Attention.ACT
    assert route.topic_key == ACTION_REQUIRED


@pytest.mark.parametrize(
    "action_kind",
    ["approval", "decision", "credential", "credits", "manual_intervention"],
)
def test_structured_human_gate_is_act(action_kind):
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {
            "agent": "scout",
            "result": "partial",
            "action_required": True,
            "action_kind": action_kind,
        },
    ))

    assert route.attention is Attention.ACT
    assert route.topic_key == ACTION_REQUIRED
    assert route.batch is False


@pytest.mark.parametrize("payload", [
    {"action_required": True},
    {"action_required": True, "action_kind": ""},
    {"action_required": True, "action_kind": "operator"},
    {"action_required": False, "action_kind": "credits"},
    {"action_kind": "credits"},
])
def test_invalid_or_incomplete_human_gate_does_not_create_act(payload):
    route = classify(make_event(
        EventType.AGENT_ITERATION,
        {"agent": "scout", "status": "failed", **payload},
    ))

    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS


# ----------------------------------------------------------- conditional hooks

def test_gateway_health_down_warns_and_pages():
    route = classify(make_event(EventType.GATEWAY_HEALTH, {"status": "down"}))
    assert route.attention is Attention.WARN
    assert route.wa_tier == WA_URGENT


def test_gateway_health_up_is_info_no_page():
    route = classify(make_event(EventType.GATEWAY_HEALTH, {"status": "up"}))
    assert route.attention is Attention.INFO
    assert route.wa_tier is None


def test_code_drift_drifting_is_warn_on_alerts():
    route = classify(make_event(
        EventType.CODE_DRIFT, {"status": "drifting", "state": "behind"}))
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS


def test_code_drift_resolved_is_info_no_page():
    route = classify(make_event(EventType.CODE_DRIFT, {"status": "resolved"}))
    assert route.attention is Attention.INFO
    assert route.wa_tier is None


def test_high_score_at_9_pages_important():
    route = classify(make_event(EventType.JOB_HIGH_SCORE, {"score": 9.2}))
    assert route.wa_tier == WA_IMPORTANT
    assert route.topic_key == JOBFLOW


def test_high_score_below_9_stays_quiet():
    route = classify(make_event(EventType.JOB_HIGH_SCORE, {"score": 8.8}))
    assert route.wa_tier is None


def test_probe_transition_recovery_is_batched_trace():
    route = classify(make_event(
        EventType.WATCHDOG_PROBE_TRANSITION,
        {"after": "healthy", "tier": "critical"},
    ))
    assert route.attention is Attention.TRACE
    assert route.wa_tier is None
    assert route.batch


def test_probe_transition_optional_tier_no_page():
    route = classify(make_event(
        EventType.WATCHDOG_PROBE_TRANSITION,
        {"after": "down", "tier": "optional"},
    ))
    assert route.wa_tier is None


def test_probe_transition_real_degradation_pages():
    route = classify(make_event(
        EventType.WATCHDOG_PROBE_TRANSITION,
        {"after": "down", "tier": "critical"},
    ))
    assert route.attention is Attention.WARN
    assert route.wa_tier == WA_URGENT


def test_burst_all_optional_no_page():
    route = classify(make_event(EventType.WATCHDOG_BURST, {
        "transitions": [
            {"after": "down", "tier": "optional"},
            {"after": "healthy", "tier": "critical"},
        ],
    }))
    assert route.wa_tier is None


def test_burst_empty_transitions_fails_open():
    route = classify(make_event(EventType.WATCHDOG_BURST, {"transitions": []}))
    assert route.wa_tier == WA_URGENT


def test_self_degraded_blackout_routes_to_security():
    route = classify(make_event(
        EventType.WATCHDOG_SELF_DEGRADED,
        {"reason": "laptop-monitor status.json stale"},
    ))
    assert route.topic_key == SECURITY


def test_self_degraded_other_reason_stays_on_alerts():
    route = classify(make_event(
        EventType.WATCHDOG_SELF_DEGRADED, {"reason": "monitor pass over budget"},
    ))
    assert route.topic_key == ALERTS


def test_silence_alert_never_pages():
    route = classify(make_event(EventType.WATCHDOG_SILENCE_ALERT))
    assert route.wa_tier is None


# ---------------------------------------------------- jobflow source demotion

def test_jobflow_agent_error_demotes_to_domain_topic():
    route = classify(make_event(
        EventType.AGENT_ERROR,
        {"source_agent": "applier"},
        source="mailbox:applier",
    ))
    assert route.topic_key == JOBFLOW
    assert route.attention is Attention.WARN


def test_jobflow_cron_prefix_demotes():
    route = classify(make_event(
        EventType.CRON_FAILED, {}, source="jobflow-tracker-cycle",
    ))
    assert route.topic_key == JOBFLOW


def test_system_cron_failure_stays_on_alerts():
    route = classify(make_event(
        EventType.CRON_FAILED, {}, source="postgres-sync",
    ))
    assert route.topic_key == ALERTS


def test_consecutive_failures_never_demote():
    """Systemic signals stay on alerts + page even for pipeline agents
    (deliberate v3 change vs the 2026-07-16 blanket demotion)."""
    route = classify(make_event(
        EventType.CRON_FAILED_CONSECUTIVE, {}, source="jobflow-scout",
    ))
    assert route.topic_key == ALERTS
    assert route.wa_tier == WA_URGENT  # CRITICAL default → WARN ∧ CRITICAL


def test_failure_cluster_never_demotes_and_pages():
    route = classify(make_event(
        EventType.AGENT_FAILURE_CLUSTER, {}, source="applier",
    ))
    assert route.topic_key == ALERTS
    assert route.wa_tier == WA_URGENT


# ----------------------------------------------------------- cron content sniff

def test_cron_red_output_upgrades_to_alerts():
    route = classify(make_event(EventType.CRON_COMPLETED, {
        "output_summary": "NIGHTLY GATE: RED - pytest rc=2 after 75s",
    }))
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS
    assert route.priority.level >= Priority.NORMAL.level


def test_cron_errors_count_upgrades():
    assert cron_output_is_alert("run done, errors=1 — first error: GET ...")


def test_cron_benign_output_stays_trace():
    route = classify(make_event(EventType.CRON_COMPLETED, {
        "output_summary": "synced 42 rows, errors=0, all green",
    }))
    assert route.attention is Attention.TRACE
    assert route.topic_key == OPS_TRACE


def test_cron_lowercase_red_word_not_matched():
    assert not cron_output_is_alert("colored the widget red for fun")


# ------------------------------------------------- cron actionable sniff

class TestCronActionableOutput:
    """A cron run that asks Diego a question is ACT, not telemetry (2026-07-27).

    Diego, reading the Telegram feed: a "CRON_COMPLETED message that require
    action ('Reply ALL or...') that should be routed somewhere else". Only
    cron_output_is_alert() existed, and it scans the HEAD 400 chars — but a
    prompt is the LAST thing a job prints, so the head window structurally
    cannot see it. These pin a TAIL-scanning actionable sniffer that outranks
    the alert sniffer.

    v3 contract: ACT ALWAYS escalates to WhatsApp (_derive_wa). Diego
    explicitly accepted that tradeoff, which is why the marker set stays
    tight — a false positive costs a phone page.
    """

    def _route(self, summary):
        return classify(make_event(EventType.CRON_COMPLETED,
                                   {"output_summary": summary}))

    def test_reply_all_prompt_routes_to_action_required(self):
        route = self._route(
            "digest built, 12 threads scanned\nReply ALL or pick a thread number.")
        assert route.attention is Attention.ACT
        assert route.topic_key == ACTION_REQUIRED

    def test_actionable_prompt_at_the_tail_is_seen(self):
        """The whole point: the alert scanner's 400-char head window would
        miss a prompt printed after a long run log."""
        summary = ("processed row\n" * 200) + "Awaiting your approval to proceed."
        assert len(summary) > 400
        assert cron_output_is_actionable(summary)
        assert self._route(summary).topic_key == ACTION_REQUIRED

    def test_actionable_beats_alert_when_output_has_both(self):
        """A run that errored AND asked a question is ACT/action_required —
        the question is the part that needs a human."""
        summary = "NIGHTLY GATE: RED - pytest rc=2\nReply ALL to retry or STOP."
        assert cron_output_is_alert(summary)
        route = self._route(summary)
        assert route.attention is Attention.ACT
        assert route.topic_key == ACTION_REQUIRED

    def test_actionable_escalates_to_whatsapp(self):
        route = self._route("done.\nACTION REQUIRED: confirm the cutover.")
        assert route.wa_tier in (WA_URGENT, WA_IMMEDIATE)

    def test_actionable_is_never_batched(self):
        assert self._route("done.\nPlease reply with a choice.").batch is False

    def test_actionable_priority_floors_at_high(self):
        route = self._route("done.\nReply YES to continue.")
        assert route.priority.level >= Priority.HIGH.level

    def test_alert_without_a_prompt_still_routes_to_alerts(self):
        """The alert path is unchanged when nothing asks a question."""
        route = self._route("NIGHTLY GATE: RED - pytest rc=2 after 75s")
        assert route.attention is Attention.WARN
        assert route.topic_key == ALERTS

    def test_benign_output_stays_trace(self):
        route = self._route("synced 42 rows, errors=0, all green")
        assert route.attention is Attention.TRACE
        assert route.topic_key == OPS_TRACE

    @pytest.mark.parametrize("summary", [
        "Reply ALL or choose one.",
        "Reply YES to continue.",
        "Respond with a thread number.",
        "ACTION REQUIRED: approve the release.",
        "Awaiting your decision.",
        "Needs your approval before the cutover.",
        "Please confirm the destination.",
        "Proceed with the merge? (y/n)",
        "Overwrite the file? [Y/n]",
    ])
    def test_recognized_prompts(self, summary):
        assert cron_output_is_actionable(summary), summary

    @pytest.mark.parametrize("summary", [
        "",
        "synced 42 rows, errors=0",
        "no reply needed, run complete",
        "the customer needs a new laptop",
        "sent 3 replies to the queue",
        "NIGHTLY GATE: RED - pytest rc=2 after 75s",
    ])
    def test_non_prompts_are_not_actionable(self, summary):
        """Prose that merely contains 'reply'/'needs' must not page a phone."""
        assert not cron_output_is_actionable(summary), summary

    def test_none_output_is_not_actionable(self):
        assert not cron_output_is_actionable(None)


# ----------------------------------------------------------- misc routing

def test_agent_iteration_routes_per_agent():
    assert classify(make_event(
        EventType.AGENT_ITERATION, {"agent": "critic"})).topic_key == CRITIC
    assert classify(make_event(
        EventType.AGENT_ITERATION, {"agent": "devflow-bridge"})).topic_key == DEVFLOW
    assert classify(make_event(
        EventType.AGENT_ITERATION, {"agent": "unknown-x"})).topic_key == JOBFLOW


def test_mailbox_notification_honors_known_to():
    route = classify(
        make_event(EventType.MAILBOX_MESSAGE,
                   {"message_type": "NOTIFICATION", "to": "markets_research"}),
        known_topic_keys={"markets_research", "scribe_daily"},
    )
    assert route.topic_key == "markets_research"


def test_mailbox_notification_unknown_to_falls_to_daily_brief():
    route = classify(
        make_event(EventType.MAILBOX_MESSAGE,
                   {"message_type": "NOTIFICATION", "to": "telegram_digests"}),
        known_topic_keys={"scribe_daily"},
    )
    assert route.topic_key == DAILY_BRIEF


def test_curator_daily_routes_to_agents_memory():
    assert classify(make_event(EventType.CURATOR_DAILY)).topic_key == AGENTS_MEMORY


def test_critic_proposal_defaults_to_critic_topic_without_paging():
    route = classify(make_event(
        EventType.CRITIC_PROPOSAL, {"summary": "advisory"},
    ))

    assert route.topic_key == CRITIC
    assert route.attention is Attention.INFO
    assert route.wa_tier is None


def test_critic_auto_applied_routes_to_critic_topic():
    route = classify(make_event(EventType.CRITIC_AUTO_APPLIED))

    assert route.topic_key == CRITIC
    assert route.attention is Attention.INFO


def test_critic_agent_iteration_routes_to_critic_topic():
    route = classify(make_event(
        EventType.AGENT_ITERATION, {"agent": "critic", "summary": "finding"},
    ))

    assert route.topic_key == CRITIC


def test_decision_required_critic_routes_once_to_action_required():
    route = classify(make_event(
        EventType.CRITIC_PROPOSAL, {"decision_required": True},
    ))

    assert route.topic_key == ACTION_REQUIRED
    assert route.attention is Attention.ACT
    assert route.batch is False


def test_build_failed_pages_urgent_from_alerts():
    route = classify(make_event(EventType.DEVFLOW_BUILD_FAILED))
    assert route.topic_key == ALERTS
    assert route.wa_tier == WA_URGENT


# ----------------------------------------------------------- topic resolution

V3_TOPICS = {
    "action_required": {"thread_id": 9637},
    "watchdog_alerts": {"thread_id": 9654},
    "jobflow_firehose": {"thread_id": 9631},
}
OLD_TOPICS = {
    "jobflow_decisions": {"thread_id": 9637},
    "critic_proposals": {"thread_id": 9663},
    "watchdog_alerts": {"thread_id": 9654},
}


def test_critic_alias_resolves_to_existing_pre_topic_thread():
    assert resolve_topic_thread(OLD_TOPICS, CRITIC) == (
        "critic_proposals", "9663",
    )


def test_resolve_direct():
    assert resolve_topic_thread(V3_TOPICS, "action_required") == (
        "action_required", "9637")


def test_resolve_alias_old_config_new_code():
    key, thread = resolve_topic_thread(OLD_TOPICS, "action_required")
    assert (key, thread) == ("jobflow_decisions", "9637")
    key, thread = resolve_topic_thread(OLD_TOPICS, "agents_memory")
    assert (key, thread) == ("critic_proposals", "9663")


def test_resolve_missing_degrades_to_alerts_not_empty():
    key, thread = resolve_topic_thread(OLD_TOPICS, "cron_firehose")
    assert (key, thread) == ("watchdog_alerts", "9654")


def test_aliases_are_acyclic():
    for start in TOPIC_ALIASES:
        seen = set()
        key = start
        while key in TOPIC_ALIASES:
            assert key not in seen
            seen.add(key)
            key = TOPIC_ALIASES[key]


def test_boot_summary_is_warn_on_alerts_without_paging():
    """laptop-start.ps1's boot report (2026-07-27) is a degraded-host signal,
    not an operator action: WARN on the alerts topic, HIGH so it survives
    significant_only/digest_only verbosity, and no WhatsApp page unless a
    caller explicitly overrides the priority to CRITICAL."""
    route = classify(make_event(
        EventType.BOOT_SUMMARY,
        {"boot_id": "20260727-132212", "state": "failed", "failed": 2},
        source="laptop-start"))
    assert route.attention is Attention.WARN
    assert route.topic_key == ALERTS
    assert route.priority is Priority.HIGH
    assert route.wa_tier is None
    assert route.batch is False


def test_boot_summary_at_critical_pages_urgent():
    """The WARN contract: an explicit --priority critical emit escalates."""
    route = classify(make_event(
        EventType.BOOT_SUMMARY, {"state": "failed"},
        priority=Priority.CRITICAL, source="laptop-start"))
    assert route.wa_tier == WA_URGENT
