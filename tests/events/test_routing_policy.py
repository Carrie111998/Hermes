"""Invariant + behavior tests for events.routing_policy (v3).

The invariant tests iterate the FULL EventType enum — adding a new event
type without a policy entry, or wiring an ACT route that doesn't page,
fails here instead of silently misrouting in production.
"""

import pytest

from events.outcomes import OutcomeState, evaluate_outcome, marker_for_verdict
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

# Alert-class types allowed to carry NO type-level verdict, each with the
# reason it is allowed. Two distinct groups, and the difference matters --
# group (a) is CORRECT and must never be "fixed", group (b) is a pending
# product decision.
#
# The set this guards drifted twice in exactly one way: a type was added
# beside an already-classified sibling and nobody classified it.
# DEVFLOW_DEPLOY_FAILED sat unclassified next to DEVFLOW_BUILD_FAILED;
# BACKEND_CONTRACT_DRIFT sat unclassified next to AGENT_LOOP_FAULT, which was
# introduced in the SAME schema comment block. Both reached the phone headed
# "UNKNOWN". This test is the thing that would have caught either on the
# commit that introduced it.
_VERDICT_EXEMPT = {
    # (a) BIDIRECTIONAL -- the verdict genuinely lives in the payload, so an
    # empty-payload UNKNOWN is the RIGHT answer. Adding any of these to
    # _FAILURE_EVENT_TYPES would render their own recoveries as red failures,
    # because `failed` wins over recovery in the precedence order.
    # test_payload_driven_exemptions_still_classify below proves each one
    # actually works, so the exemption cannot hide a regression.
    EventType.CODE_DRIFT: "bidirectional: status=='resolved' is a recovery",
    EventType.WATCHDOG_PROBE_TRANSITION: "bidirectional: payload.after",
    EventType.WATCHDOG_BURST: "bidirectional: payload.transitions list",
    EventType.MODEL_RATE_LIMITED: "bidirectional: outcome=='recovered'",
    # (b) NOT FAILURES -- good news that needs a human, or a governance flag.
    # These are ACT/WARN because they need attention, not because anything
    # broke. Giving them a failure verdict would be wrong; whether they should
    # carry some OTHER label is an open product decision (2026-08-20).
    EventType.INTERVIEW_SIGNAL: "good news needing action; labelling undecided",
    EventType.OFFER_SIGNAL: "good news needing action; labelling undecided",
    EventType.FOLLOWUP_DUE: "a reminder, not a failure; labelling undecided",
    EventType.DEVFLOW_AUTO_MERGED: "governance flag on an ungated merge, not a failure",
}

# Representative payloads proving the group (a) exemptions really do classify.
_PAYLOAD_DRIVEN_CASES = {
    EventType.CODE_DRIFT: {"status": "resolved"},
    EventType.WATCHDOG_PROBE_TRANSITION: {"before": "healthy", "after": "down"},
    EventType.WATCHDOG_BURST: {"transitions": [{"after": "healthy", "tier": "critical"}]},
    EventType.MODEL_RATE_LIMITED: {"outcome": "chain_exhausted"},
}


@pytest.mark.parametrize("et", list(EventType))
def test_every_alerting_event_type_can_describe_itself(et):
    """An event the system will wake a human for must be able to say whether
    it is good or bad. WARN/ACT with an UNKNOWN type-level verdict means the
    header renders "UNKNOWN <TYPE>" -- which is what reached the phone for
    watchdog_probe_transition, container_crash_loop, devflow.deploy_failed,
    secret_detected, credential_loss, backend_contract_drift and boot_summary
    before 2026-08-19/20.

    A NEW event type added with a wa_tier and no verdict fails here, on the
    commit that adds it, instead of on Diego's phone.
    """
    route = classify(make_event(et))
    if route.attention not in (Attention.WARN, Attention.ACT):
        return
    if evaluate_outcome(make_event(et)).state is not OutcomeState.UNKNOWN:
        return
    assert et in _VERDICT_EXEMPT, (
        f"{et.type_string} is {route.attention.name}-class (wa_tier="
        f"{route.wa_tier}) but has no type-level verdict, so its header will "
        f"read 'UNKNOWN {et.type_string.upper()}'. Either classify it in "
        f"events.outcomes, or add it to _VERDICT_EXEMPT with the reason."
    )


@pytest.mark.parametrize("et", sorted(_VERDICT_EXEMPT, key=lambda e: e.type_string))
def test_no_stale_verdict_exemptions(et):
    """An exemption that stops being true must be deleted, not left to rot.
    Without this, classifying a type later leaves a stale entry that would
    silently excuse a future regression on the same type."""
    route = classify(make_event(et))
    assert route.attention in (Attention.WARN, Attention.ACT), (
        f"{et.type_string} is no longer alert-class; drop it from _VERDICT_EXEMPT"
    )
    assert evaluate_outcome(make_event(et)).state is OutcomeState.UNKNOWN, (
        f"{et.type_string} now HAS a type-level verdict; drop it from "
        f"_VERDICT_EXEMPT so the guard applies to it again"
    )


@pytest.mark.parametrize("et", sorted(_PAYLOAD_DRIVEN_CASES, key=lambda e: e.type_string))
def test_payload_driven_exemptions_still_classify(et):
    """The group (a) exemptions are only defensible because these types DO
    reach a verdict from a real payload. Asserting that turns each exemption
    from a blanket excuse into a proof, so a broken payload rule cannot hide
    behind the allowlist."""
    verdict = evaluate_outcome(make_event(et, payload=_PAYLOAD_DRIVEN_CASES[et]))

    assert verdict.state is not OutcomeState.UNKNOWN, (
        f"{et.type_string} is exempt as payload-driven, but its representative "
        f"payload still yields UNKNOWN -- the payload rule is broken"
    )


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


# --------------------------------------------- jobflow cron telemetry split

class TestJobflowCronTelemetrySplit:
    """Plain-success JobFlow run-summaries belong in the JobFlow topic, not
    the ops firehose (2026-08-06 operator request: an 11-event mixed batch
    of JobFlow summaries + postgres-sync + inbox sweeps was landing in Ops
    Trace). NARROW allowlist by operator choice — only the run-summary jobs
    Diego reads move; shadow/enrich/archive/soak/sweeper runs stay in ops.

    The redirect touches ONLY plain-success telemetry: the actionable and
    error-content upgrades (→ Action Required / Alerts) still outrank it.
    """

    def _route(self, source, summary="run complete: 3 jobs scored, 0 errors"):
        return classify(make_event(
            EventType.CRON_COMPLETED,
            {"job_name": source, "output_summary": summary},
            source=source,
        ))

    @pytest.mark.parametrize("job", [
        "jobflow-scout",
        "jobflow-matcher",
        "jobflow-tracker-cycle",
        "jaum-daytime-relay",
    ])
    def test_jobflow_success_routes_to_jobflow_topic(self, job):
        route = self._route(job)
        assert route.topic_key == JOBFLOW
        assert route.attention is Attention.TRACE
        assert route.batch is True
        assert route.wa_tier is None

    @pytest.mark.parametrize("job", [
        "postgres-sync",
        "jaum-inbox-sweeper",
        "jobflow-matcher-shadow",
        "jobflow-archiver",
        "jobflow-pipeline-worker-soak-recheck",
        "jobflow-ats-url-resolve",
        "langfuse-retention-sweep",
    ])
    def test_ops_and_housekeeping_stay_in_ops_trace(self, job):
        route = self._route(job)
        assert route.topic_key == OPS_TRACE
        assert route.attention is Attention.TRACE

    def test_jobflow_source_routes_even_without_job_name_in_payload(self):
        """The redirect keys off event.source too, not only payload.job_name."""
        route = classify(make_event(
            EventType.CRON_COMPLETED,
            {"output_summary": "8 jobs scored"},
            source="jobflow-scout",
        ))
        assert route.topic_key == JOBFLOW

    def test_actionable_beats_jobflow_redirect(self):
        """A JobFlow run that asks a question is still ACT — the redirect
        must not swallow an operator prompt into the JobFlow firehose."""
        route = self._route(
            "jobflow-scout",
            summary="scout done.\nReply ALL to approve the 3 VIP finds.",
        )
        assert route.attention is Attention.ACT
        assert route.topic_key == ACTION_REQUIRED

    def test_error_content_beats_jobflow_redirect(self):
        """A JobFlow run with error output is still WARN/Alerts."""
        route = self._route(
            "jobflow-matcher",
            summary="matcher run: errors=3 — first error: GET /score rc=28",
        )
        assert route.attention is Attention.WARN
        assert route.topic_key == ALERTS

    def test_followup_and_weekly_stay_in_ops_trace(self):
        """Narrow scope: only tracker-cycle moves; followup/weekly do not."""
        assert self._route("jobflow-tracker-followup").topic_key == OPS_TRACE
        assert self._route("jobflow-tracker-weekly").topic_key == OPS_TRACE


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


# ------------------------------------------------- disk pressure (2026-08-14)

def test_disk_critical_pages_and_lands_in_action_required():
    """A disk about to hit zero needs a human, not an alerts-thread line.

    Regression: disk_low fired on 11 days between 07-17 and 08-14 -- five of
    them at 0.0 GB free -- and was delivered every time into `watchdog_alerts`
    alongside ~2,200 watchdog events. It was never seen in time.
    """
    route = classify(make_event(
        EventType.RESOURCE_PRESSURE,
        payload={"reasons": ["disk_low", "disk_critical"], "disk_c_free_gb": 8.6},
    ))
    assert route.attention is Attention.ACT
    assert route.topic_key == ACTION_REQUIRED
    assert route.wa_tier in (WA_URGENT, WA_IMMEDIATE)


def test_disk_low_alone_warns_without_paging():
    """The 60 GB early-warning axis must stay cheap to receive.

    Paging on it would fire every cooldown through a whole day of ordinary
    Docker churn and train exactly the alert-blindness this is meant to fix.
    """
    route = classify(make_event(
        EventType.RESOURCE_PRESSURE,
        payload={"reasons": ["disk_low"], "disk_c_free_gb": 55.0},
    ))
    assert route.attention is Attention.WARN
    assert route.topic_key != ACTION_REQUIRED
    assert route.wa_tier is None


def test_non_disk_pressure_is_unchanged():
    route = classify(make_event(
        EventType.RESOURCE_PRESSURE,
        payload={"reasons": ["commit_high"], "commit_pct": 98.4},
    ))
    assert route.attention is Attention.WARN


# ------------------------------------------------------------- AGENT_NOTE
#
# AGENT_NOTE is the one type with no intrinsic semantics, so the caller
# declares its attention class in the payload. These tests pin the two
# things that make that safe: the class ceiling, and the fact that NOTHING
# in a caller-supplied payload can lift a note onto the phone.

class TestAgentNoteRouting:
    def test_default_is_info_on_agents_memory(self):
        route = classify(make_event(EventType.AGENT_NOTE, {"headline": "hi"}))
        assert route.attention is Attention.INFO
        assert route.topic_key == AGENTS_MEMORY
        assert route.wa_tier is None

    def test_warn_lands_on_alerts_at_normal_or_higher(self):
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "attention": "warn"}))
        assert route.attention is Attention.WARN
        assert route.topic_key == ALERTS
        assert route.priority.level >= Priority.NORMAL.level
        assert route.wa_tier is None

    def test_trace_batches_on_the_ops_firehose(self):
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "attention": "trace"}))
        assert route.attention is Attention.TRACE
        assert route.topic_key == OPS_TRACE
        assert route.batch is True

    def test_unrecognised_attention_falls_back_to_info(self):
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "attention": "banana"}))
        assert route.attention is Attention.INFO
        assert route.topic_key == AGENTS_MEMORY

    def test_attention_is_case_and_whitespace_tolerant(self):
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "attention": "  WARN "}))
        assert route.attention is Attention.WARN

    def test_act_is_clamped_to_warn_and_never_pages(self):
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "attention": "act"}))
        assert route.attention is Attention.WARN
        assert route.topic_key == ALERTS
        assert route.wa_tier is None

    def test_structured_human_gate_cannot_smuggle_a_note_into_act(self):
        """action_required + action_kind promotes ANY event to ACT/page
        (routing_policy.structured_human_gate). The AGENT_NOTE clamp runs
        AFTER that gate precisely so a caller-supplied payload cannot buy a
        phone page. Placed before the gate, this test fails."""
        route = classify(make_event(EventType.AGENT_NOTE, {
            "headline": "hi",
            "action_required": True,
            "action_kind": "approval",
        }))
        assert route.attention is not Attention.ACT
        assert route.topic_key != ACTION_REQUIRED
        assert route.wa_tier is None

    def test_critical_priority_is_capped_to_high_and_still_never_pages(self):
        """The closed valve: WARN + CRITICAL would otherwise derive
        WA_URGENT in _derive_wa."""
        route = classify(make_event(
            EventType.AGENT_NOTE,
            {"headline": "hi", "attention": "warn"},
            priority=Priority.CRITICAL,
        ))
        assert route.priority is Priority.HIGH
        assert route.wa_tier is None

    def test_failed_status_still_never_pages(self):
        """status: failed promotes an INFO note to WARN/Alerts via the
        verdict machinery. That promotion is intended; a page is not."""
        route = classify(make_event(
            EventType.AGENT_NOTE, {"headline": "hi", "status": "failed"}))
        assert route.attention is Attention.WARN
        assert route.topic_key == ALERTS
        assert route.wa_tier is None

    @pytest.mark.parametrize("payload", [
        {"headline": "h", "attention": "act", "action_required": True,
         "action_kind": "credential"},
        {"headline": "h", "attention": "act", "status": "failed"},
        {"headline": "h", "attention": "warn", "action_required": True,
         "action_kind": "decision", "status": "error"},
    ])
    def test_no_payload_combination_ever_escalates(self, payload):
        for priority in Priority:
            route = classify(make_event(
                EventType.AGENT_NOTE, payload, priority=priority))
            assert route.wa_tier is None, (payload, priority)
            assert route.priority.level <= Priority.HIGH.level


def test_agent_note_never_escalates_via_the_whatsapp_adapter():
    """classify_tier() is a thin adapter over classify(); asserting through
    it proves the escalator itself stays silent, which is why the design
    adds no whatsapp_escalator branch."""
    from events.subscribers.whatsapp_escalator import classify_tier
    for priority in Priority:
        event = make_event(
            EventType.AGENT_NOTE,
            {"headline": "h", "attention": "act", "action_required": True,
             "action_kind": "approval"},
            priority=priority,
        )
        assert classify_tier(event) is None
# --- critical-tier outages render red (2026-08-19) --------------------------
# 160ed2d477 made a transition-to-unhealthy verdict FAILED, but the header
# still wore 🟠: marker_for_verdict reserves 🔴 for Priority.CRITICAL and
# probe transitions arrive at HIGH. Diego asked for critical-tier outages to
# read red, which means flooring the PRIORITY, not special-casing the marker.
#
# The safety property is that this must NOT change escalation. The policy
# entry pins wa=WA_URGENT explicitly, and _derive_wa returns an explicit pin
# unchanged, so wa_tier stays "urgent" and quiet hours still queue it. If a
# future edit drops that pin, CRITICAL would derive a different tier and a
# 3am disk-space blip would break through — test_critical_tier_outage_does_
# not_become_a_quiet_hours_breakthrough is what catches that.


def _probe(after, tier="critical", before="healthy", priority=Priority.HIGH):
    return Event.create(
        event_type=EventType.WATCHDOG_PROBE_TRANSITION,
        source="watchdog",
        payload={
            "watchdog_type": "watchdog_probe_transition",
            "probe": "Hermes API Server :8642",
            "tier": tier,
            "category": "hermes",
            "before": before,
            "after": after,
            "detail": "",
        },
        priority=priority,
    )


def test_critical_tier_outage_is_floored_to_critical():
    route = classify(_probe("down"))

    assert route.verdict.state is OutcomeState.FAILED
    assert route.priority is Priority.CRITICAL


def test_critical_tier_outage_renders_red():
    route = classify(_probe("down"))

    assert marker_for_verdict(route.verdict, route.priority) == "🔴"


def test_critical_tier_outage_does_not_become_a_quiet_hours_breakthrough():
    """The whole point of flooring priority was the DOT. Escalation must not
    move: WA_URGENT is queued during quiet hours, WA_IMMEDIATE is not."""
    route = classify(_probe("down"))

    assert route.wa_tier == WA_URGENT


def test_non_critical_tier_outage_is_not_floored():
    """An 'important' or 'optional' probe going down is still a failure, but
    it is not a red-alert outage -- 159 of the 301 real probe transitions on
    this box are tier=important, so over-flooring would repaint most of them."""
    for tier in ("important", "optional"):
        route = classify(_probe("down", tier=tier))

        assert route.verdict.state is OutcomeState.FAILED
        assert route.priority is Priority.HIGH
        assert marker_for_verdict(route.verdict, route.priority) == "🟠"


def test_critical_tier_recovery_is_not_floored():
    """Only the FAILED direction is an outage. A recovery must not inherit
    the red-alert priority just because the probe is critical-tier."""
    route = classify(_probe("healthy", before="down"))

    assert route.verdict.state is OutcomeState.RECOVERED
    assert route.priority is not Priority.CRITICAL
    assert marker_for_verdict(route.verdict, route.priority) == "🟢"


def test_critical_tier_degraded_is_not_floored():
    """DEGRADED is a partial outage, not a red alert."""
    route = classify(_probe("degraded"))

    assert route.verdict.state is OutcomeState.DEGRADED
    assert route.priority is not Priority.CRITICAL
