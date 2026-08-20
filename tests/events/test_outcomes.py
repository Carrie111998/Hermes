"""Failure-wins outcome normalization contract."""

import pytest

from events.outcomes import (
    FailureKind,
    OutcomeState,
    evaluate_outcome,
    marker_for_verdict,
)
from events.schema import Event, EventType, Priority


def _event(
    payload: dict,
    event_type: EventType = EventType.AGENT_ITERATION,
    priority: Priority = Priority.LOW,
) -> Event:
    return Event.create(
        event_type=event_type,
        source="test",
        payload=payload,
        priority=priority,
    )


@pytest.mark.parametrize(
    ("payload", "state", "kind", "evidence_path"),
    [
        (
            {"counters": {"exit_code": 1}, "reason": "success"},
            OutcomeState.FAILED,
            FailureKind.OTHER,
            "payload.counters.exit_code",
        ),
        (
            {"exit_code": 2},
            OutcomeState.FAILED,
            FailureKind.OTHER,
            "payload.exit_code",
        ),
        (
            {"message_type": "ERROR", "message": "connection refused"},
            OutcomeState.FAILED,
            FailureKind.CONNECTION,
            "payload.message_type",
        ),
        (
            {"status": "FAILED", "error": "validation rejected"},
            OutcomeState.FAILED,
            FailureKind.VALIDATION,
            "payload.status",
        ),
        (
            {"timeout": True, "phase": "whole_suite"},
            OutcomeState.FAILED,
            FailureKind.TIMEOUT,
            "payload.timeout",
        ),
        (
            {"result": "timed_out", "phase": "browser_navigation"},
            OutcomeState.FAILED,
            FailureKind.TIMEOUT,
            "payload.result",
        ),
        (
            {"reason": "partial"},
            OutcomeState.DEGRADED,
            None,
            "payload.reason",
        ),
        (
            {"status": "degraded"},
            OutcomeState.DEGRADED,
            None,
            "payload.status",
        ),
        (
            {"status": "pending"},
            OutcomeState.PENDING,
            None,
            "payload.status",
        ),
        (
            {"decision_required": True},
            OutcomeState.PENDING,
            None,
            "payload.decision_required",
        ),
        (
            {"status": "healthy", "before": "down"},
            OutcomeState.RECOVERED,
            None,
            "payload.status",
        ),
        (
            {"reason": "success", "counters": {"exit_code": 0}},
            OutcomeState.SUCCEEDED,
            None,
            "payload.reason",
        ),
        (
            {"status": "up"},
            OutcomeState.SUCCEEDED,
            None,
            "payload.status",
        ),
        (
            {"reason": "no_work"},
            OutcomeState.NO_WORK,
            None,
            "payload.reason",
        ),
        (
            {"summary": "ordinary telemetry"},
            OutcomeState.UNKNOWN,
            None,
            "event",
        ),
    ],
)
def test_evidence_shapes(payload, state, kind, evidence_path):
    verdict = evaluate_outcome(_event(payload))

    assert verdict.state is state
    assert verdict.failure_kind is kind
    assert any(item.path == evidence_path for item in verdict.evidence)


def test_failure_outranks_success_and_recovery():
    verdict = evaluate_outcome(
        _event(
            {
                "reason": "success",
                "status": "healthy",
                "before": "down",
                "counters": {"exit_code": 1},
            }
        )
    )

    assert verdict.state is OutcomeState.FAILED
    assert any(
        item.path == "payload.counters.exit_code" for item in verdict.evidence
    )


def test_degraded_outranks_pending_and_success():
    verdict = evaluate_outcome(
        _event(
            {
                "result": "partial",
                "status": "pending",
                "reason": "success",
            }
        )
    )

    assert verdict.state is OutcomeState.DEGRADED


@pytest.mark.parametrize(
    ("event_type", "state"),
    [
        (EventType.CRON_FAILED, OutcomeState.FAILED),
        (EventType.APPLICATION_FAILED, OutcomeState.FAILED),
        (EventType.AGENT_FAILURE_CLUSTER, OutcomeState.FAILED),
        (EventType.NOTIFICATION_FAILED, OutcomeState.FAILED),
        (EventType.CRON_STALE, OutcomeState.DEGRADED),
        (EventType.RESOURCE_PRESSURE, OutcomeState.DEGRADED),
        (EventType.WATCHDOG_SELF_DEGRADED, OutcomeState.DEGRADED),
        (EventType.APPROVAL_REQUEST, OutcomeState.PENDING),
        (EventType.APPLICATION_BLOCKED, OutcomeState.PENDING),
        (EventType.CRON_COMPLETED, OutcomeState.SUCCEEDED),
        (EventType.APPLICATION_SUBMITTED, OutcomeState.SUCCEEDED),
        (EventType.DEVFLOW_BUILD_SUCCEEDED, OutcomeState.SUCCEEDED),
        (EventType.NOTIFICATION_DELIVERED, OutcomeState.SUCCEEDED),
        (EventType.GATEWAY_STARTED, OutcomeState.RECOVERED),
        (EventType.WATCHDOG_RECOVERED, OutcomeState.RECOVERED),
    ],
)
def test_explicit_event_types_are_outcome_evidence(event_type, state):
    verdict = evaluate_outcome(_event({}, event_type=event_type))

    assert verdict.state is state
    assert any(item.path == "event.event_type" for item in verdict.evidence)


def test_cron_completed_legacy_failure_marker_is_compatibility_evidence():
    verdict = evaluate_outcome(
        _event(
            {"output_summary": "NIGHTLY GATE: RED - pytest rc=124 after 2700s"},
            event_type=EventType.CRON_COMPLETED,
            priority=Priority.NORMAL,
        )
    )

    assert verdict.state is OutcomeState.FAILED
    assert verdict.failure_kind is FailureKind.TIMEOUT
    assert any(item.path == "payload.output_summary" for item in verdict.evidence)


def test_failure_and_degradation_have_high_priority_floor():
    assert evaluate_outcome(_event({"exit_code": 1})).priority_floor is Priority.HIGH
    assert evaluate_outcome(_event({"status": "partial"})).priority_floor is Priority.HIGH


def _burst(transitions, priority: Priority = Priority.HIGH) -> Event:
    return _event(
        {"transitions": transitions},
        event_type=EventType.WATCHDOG_BURST,
        priority=priority,
    )


def test_watchdog_burst_recovery_only_is_recovered():
    """A burst whose every change is a recovery is good news, not UNKNOWN.

    WATCHDOG_BURST carries its state in a ``transitions`` list, not a
    top-level ``after``/``status``, so the plain recovery detector never
    saw it and a recovery-only sweep fell through to UNKNOWN -> yellow.
    """
    verdict = evaluate_outcome(
        _burst(
            [
                {"after": "healthy", "tier": "critical"},
                {"after": "healthy", "tier": "optional"},
            ]
        )
    )

    assert verdict.state is OutcomeState.RECOVERED
    assert any(item.path == "payload.transitions" for item in verdict.evidence)


def test_watchdog_burst_recovery_only_marker_is_green():
    verdict = evaluate_outcome(_burst([{"after": "healthy", "tier": "critical"}]))

    assert marker_for_verdict(verdict, Priority.HIGH) == "🟢"


def test_watchdog_burst_with_failure_is_not_recovered():
    """A burst that still has a down probe must never read as recovered/green."""
    verdict = evaluate_outcome(
        _burst(
            [
                {"after": "down", "tier": "critical"},
                {"after": "healthy", "tier": "optional"},
            ]
        )
    )

    assert verdict.state is not OutcomeState.RECOVERED
    assert marker_for_verdict(verdict, Priority.HIGH) != "🟢"


def test_watchdog_burst_skips_only_is_not_recovered():
    """All-skipped sweeps (monitor over budget) are not a recovery."""
    verdict = evaluate_outcome(
        _burst([{"after": "unknown", "tier": "critical"}])
    )

    assert verdict.state is not OutcomeState.RECOVERED


@pytest.mark.parametrize(
    ("payload", "priority", "expected"),
    [
        ({"exit_code": 1}, Priority.CRITICAL, "🔴"),
        ({"exit_code": 1}, Priority.HIGH, "🟠"),
        ({"status": "partial"}, Priority.HIGH, "🟠"),
        ({"status": "pending"}, Priority.HIGH, "🟡"),
        ({"status": "healthy"}, Priority.HIGH, "🟢"),
        ({"reason": "success"}, Priority.NORMAL, "🟢"),
        ({"summary": "tick"}, Priority.LOW, "🟡"),
    ],
)
def test_marker_table(payload, priority, expected):
    verdict = evaluate_outcome(_event(payload, priority=priority))

    assert marker_for_verdict(verdict, priority) == expected


def test_no_work_reason_is_green_not_unknown():
    verdict = evaluate_outcome(_event({"agent": "critic", "reason": "no_work"}))
    assert verdict.state is OutcomeState.NO_WORK
    assert marker_for_verdict(verdict, Priority.LOW) == "🟢"


def test_no_work_yields_to_failure_evidence():
    verdict = evaluate_outcome(
        _event({"agent": "critic", "reason": "no_work", "status": "error"})
    )
    assert verdict.state is OutcomeState.FAILED
# --- transition-to-unhealthy is a FAILURE (2026-08-19) ----------------------
# outcomes.py taught the classifier to read `payload.after` for exactly ONE
# value: WATCHDOG_PROBE_TRANSITION + after == "healthy" -> RECOVERED. The
# mirror never existed, so a critical probe going healthy -> down produced no
# evidence at all and fell through to UNKNOWN -> yellow. Observed live
# 2026-08-19: a real ":8642 down" alert reached the phone headed
# "UNKNOWN SYSTEM HEALTH ALERT". Same fall-through the burst tests above
# already fixed for recoveries; these pin the failure direction, and pin it
# for EVERY event type rather than one more per-type branch.


def _transition(after, before="healthy", event_type=EventType.WATCHDOG_PROBE_TRANSITION,
                priority=Priority.HIGH):
    return _event(
        {
            "watchdog_type": "watchdog_probe_transition",
            "probe": "Hermes API Server :8642",
            "tier": "critical",
            "category": "hermes",
            "before": before,
            "after": after,
            "detail": "",
        },
        event_type=event_type,
        priority=priority,
    )


def test_probe_transition_to_down_is_failed():
    """The exact payload observed live on 2026-08-19 at 02:10:50Z."""
    verdict = evaluate_outcome(_transition("down"))

    assert verdict.state is OutcomeState.FAILED
    assert any(item.path == "payload.after" for item in verdict.evidence)


def test_probe_transition_to_healthy_is_still_recovered():
    """Regression guard: the failure rule must not shadow the recovery rule."""
    verdict = evaluate_outcome(_transition("healthy", before="down"))

    assert verdict.state is OutcomeState.RECOVERED
    # Either recovery rule may claim it: the generic healthy_transition
    # (line 204) fires first when `before` is unhealthy, the probe-specific
    # probe_recovered otherwise. The STATE is the contract, not which rule won.
    assert any(
        item.code in ("probe_recovered", "healthy_transition")
        for item in verdict.evidence
    )


def test_probe_transition_to_down_floors_priority_high():
    """FAILED carries priority_floor HIGH; UNKNOWN carried None, so a
    low-priority producer could not be floored up before this."""
    verdict = evaluate_outcome(_transition("down", priority=Priority.LOW))

    assert verdict.priority_floor is Priority.HIGH


@pytest.mark.parametrize("priority,marker", [
    (Priority.CRITICAL, "🔴"),
    (Priority.HIGH, "🟠"),
])
def test_probe_transition_to_down_marker_is_not_ambiguous(priority, marker):
    """Never 🟡 again — that is the marker for genuinely unclassifiable."""
    verdict = evaluate_outcome(_transition("down"))

    assert marker_for_verdict(verdict, priority) == marker


def test_transition_to_unhealthy_is_generic_not_watchdog_only():
    """The rule is keyed on payload.after for ALL event types, not pinned to
    WATCHDOG_PROBE_TRANSITION -- that per-type pinning is what left the gap."""
    verdict = evaluate_outcome(
        _transition("down", event_type=EventType.GATEWAY_HEALTH)
    )

    assert verdict.state is OutcomeState.FAILED


def test_transition_to_degraded_is_degraded_not_failed():
    """_UNHEALTHY_VALUES contains "degraded", so keying the failure rule on
    that set would over-escalate a partial outage to a hard failure. The
    rule classifies `after` through the same value sets `status` uses."""
    verdict = evaluate_outcome(_transition("degraded"))

    assert verdict.state is OutcomeState.DEGRADED


def test_transition_to_unknown_stays_unknown():
    """"unknown" means the probe was SKIPPED, not that it failed -- the same
    reading watchdog_burst_body already applies to its transitions list."""
    verdict = evaluate_outcome(_transition("unknown"))

    assert verdict.state is OutcomeState.UNKNOWN


def test_watchdog_burst_still_uses_its_transitions_list():
    """A burst has no top-level `after`, so the new rule must not disturb it."""
    verdict = evaluate_outcome(_burst([{"after": "healthy", "tier": "critical"}]))

    assert verdict.state is OutcomeState.RECOVERED
# --- monodirectional bad-news types carry their verdict in the TYPE ---------
# CONTAINER_CRASH_LOOP and DEVFLOW_DEPLOY_FAILED were in none of the four
# evidence sets and carry no _VALUE_FIELDS key and no `after`, so both fell
# through to UNKNOWN. Observed live 2026-08-19: a real hindsight-app crash
# loop (1085 restarts in 24h against a budget of 20) reached the phone headed
# "UNKNOWN CONTAINER_CRASH_LOOP".
#
# These are TYPE-level, unlike the transition rule above: the event only ever
# exists as bad news (watchdog_sweep.py:854 emits it from a variable named
# `alarm`; there is no recovery variant -- WATCHDOG_RECOVERED covers that).
# DEVFLOW_BUILD_FAILED was already classified and DEVFLOW_DEPLOY_FAILED was
# not, which is an omission rather than a decision.


def test_container_crash_loop_is_failed():
    """The real payload observed live 2026-08-19T03:56:12Z."""
    verdict = evaluate_outcome(
        _event(
            {
                "watchdog_type": "container_crash_loop",
                "container": "hindsight-app",
                "restarts_24h": 1085,
                "restart_count_now": 893,
                "threshold": 20,
                "tray_state": "down",
                "tray_tier": "important",
                "tray_detail": "CHURNING (running but crash-looping)",
            },
            event_type=EventType.CONTAINER_CRASH_LOOP,
            priority=Priority.HIGH,
        )
    )

    assert verdict.state is OutcomeState.FAILED
    assert verdict.priority_floor is Priority.HIGH


def test_container_crash_loop_is_failed_even_when_the_tray_reads_healthy():
    """THE TRAP, pinned. Keying this verdict on payload.tray_state is the
    obvious fix and it is WRONG: laptop-monitor's churn verdict is a one-pass
    RestartCount delta that self-clears 600s after the last restart, so a
    container that restarted 264 times in a morning renders tray_state
    "healthy" and green (observed 2026-08-10T08:15, hindsight-app). A
    tray_state-driven rule would call a REAL crash loop healthy for exactly
    the cases this alert exists to catch. The verdict comes from the TYPE.
    """
    verdict = evaluate_outcome(
        _event(
            {
                "watchdog_type": "container_crash_loop",
                "container": "hindsight-app",
                "restarts_24h": 264,
                "threshold": 20,
                "tray_state": "healthy",
                "tray_detail": "running, RestartCount stable (266)",
            },
            event_type=EventType.CONTAINER_CRASH_LOOP,
            priority=Priority.HIGH,
        )
    )

    assert verdict.state is OutcomeState.FAILED


def test_devflow_deploy_failed_is_failed_like_its_build_sibling():
    """DEVFLOW_BUILD_FAILED was classified and DEVFLOW_DEPLOY_FAILED was not.
    Two siblings, both named 'failed'."""
    for et in (EventType.DEVFLOW_BUILD_FAILED, EventType.DEVFLOW_DEPLOY_FAILED):
        verdict = evaluate_outcome(_event({}, event_type=et, priority=Priority.HIGH))

        assert verdict.state is OutcomeState.FAILED, et


@pytest.mark.parametrize("event_type", [
    EventType.CONTAINER_CRASH_LOOP,
    EventType.DEVFLOW_DEPLOY_FAILED,
])
def test_monodirectional_failures_never_wear_the_ambiguous_marker(event_type):
    verdict = evaluate_outcome(_event({}, event_type=event_type, priority=Priority.HIGH))

    assert marker_for_verdict(verdict, Priority.HIGH) != "🟡"
# --- security-posture losses are failures too (2026-08-20) ------------------
# SECRET_DETECTED and CREDENTIAL_LOSS were the last two monodirectional
# bad-news types left UNKNOWN. Both are ACT-class with wa=immediate -- they
# break quiet hours to wake a human -- yet the header could not say whether
# they were good or bad.
#
# Type-level is safe here because neither has a recovery variant, which
# MATTERS: `failed` wins over `recovery` in the precedence order, so making a
# bidirectional type a member would render its recoveries as red failures.
# Confirmed for CREDENTIAL_LOSS at BOTH producers -- watchdog_sweep emits only
# the healthy -> down/error edge (_CREDENTIAL_LOSS_BAD_STATES, plus a
# before=="healthy" guard), and devflow_pr_build_poller._check_auth_transition
# says outright "Recovery is logged, not emitted". SECRET_DETECTED is "a
# secret being *found*" (schema.py:207).


@pytest.mark.parametrize("event_type", [
    EventType.SECRET_DETECTED,
    EventType.CREDENTIAL_LOSS,
])
def test_security_posture_losses_are_failed(event_type):
    verdict = evaluate_outcome(_event({}, event_type=event_type))

    assert verdict.state is OutcomeState.FAILED
    assert verdict.priority_floor is Priority.HIGH


def test_credential_loss_is_failed_on_the_dead_token_shape_too():
    """The watchdog shape carries after="down", which the transition rule in
    160ed2d477 already caught. The devflow poller's dead-GitHub-token shape
    does not, and fell through to UNKNOWN until the type carried the verdict.
    """
    verdict = evaluate_outcome(
        _event(
            {"watchdog_type": "credential_loss", "probe": "GitHub token",
             "after": "dead", "detail": "401 Bad credentials"},
            event_type=EventType.CREDENTIAL_LOSS,
        )
    )

    assert verdict.state is OutcomeState.FAILED


def test_credential_loss_real_watchdog_payload_is_failed():
    """The real 2026 payload: Hermes OAuth token validity, healthy -> down."""
    verdict = evaluate_outcome(
        _event(
            {"watchdog_type": "credential_loss",
             "probe": "Hermes OAuth token validity", "tier": "critical",
             "category": "hermes", "before": "healthy", "after": "down",
             "detail": "no active provider resolvable (profile store)"},
            event_type=EventType.CREDENTIAL_LOSS,
        )
    )

    assert verdict.state is OutcomeState.FAILED


@pytest.mark.parametrize("event_type", [
    EventType.SECRET_DETECTED,
    EventType.CREDENTIAL_LOSS,
])
def test_security_posture_losses_never_wear_the_ambiguous_marker(event_type):
    verdict = evaluate_outcome(_event({}, event_type=event_type))

    assert marker_for_verdict(verdict, Priority.CRITICAL) == "🔴"
# --- backend drift + rate-limit outcomes (2026-08-20) -----------------------
# The last two alert-class UNKNOWNs worth fixing, and they need DIFFERENT
# fixes -- which is the point. The empty-payload sweep flags both the same
# way; only replaying real payloads shows that one is a monodirectional TYPE
# and the other is a bidirectional type with unrecognized VALUES.


def test_backend_contract_drift_is_failed():
    """Monodirectional, like its sibling AGENT_LOOP_FAULT (same schema comment
    block, already classified). The canary fires when drift IS found; there is
    no drift-resolved variant -- contrast CODE_DRIFT, which has one."""
    verdict = evaluate_outcome(
        _event(
            {"detector": "backend_conformance_canary", "backend": "codex",
             "signature": "output=None"},
            event_type=EventType.BACKEND_CONTRACT_DRIFT,
        )
    )

    assert verdict.state is OutcomeState.FAILED


def _rate_limited(outcome, reason="quota_window"):
    return _event(
        {"provider": "deepseek", "model": "deepseek-v4-pro", "reason": reason,
         "outcome": outcome, "detector": "runtime", "diverted_calls": 1},
        event_type=EventType.MODEL_RATE_LIMITED,
        priority=Priority.HIGH,
    )


@pytest.mark.parametrize("outcome", ["chain_exhausted", "no_fallback"])
def test_rate_limit_outcomes_with_no_model_left_are_failed(outcome):
    """Both mean no model is available. formatting.py already words them apart
    because the REMEDY differs (every alternative also down vs none ever
    configured), but the verdict is the same."""
    verdict = evaluate_outcome(_rate_limited(outcome))

    assert verdict.state is OutcomeState.FAILED


def test_rate_limit_diverted_is_degraded_not_failed():
    """The fallback WORKED -- running on a second-choice model is worse, not
    broken. Calling it FAILED would cry wolf on the successful mitigation."""
    verdict = evaluate_outcome(_rate_limited("diverted", reason="rate_limit"))

    assert verdict.state is OutcomeState.DEGRADED


def test_rate_limit_recovered_survives_the_new_failure_values():
    """THE REGRESSION GUARD, and the reason MODEL_RATE_LIMITED must NOT be
    added to _FAILURE_EVENT_TYPES. 5 of 44 real events carry outcome=
    "recovered" and already classified as RECOVERED before this change; a
    type-level rule would have turned them red, because `failed` wins over
    recovery in the precedence order.
    """
    verdict = evaluate_outcome(_rate_limited("recovered", reason="recovered"))

    assert verdict.state is OutcomeState.RECOVERED


@pytest.mark.parametrize("outcome", ["chain_exhausted", "no_fallback", "diverted"])
def test_rate_limit_outcomes_never_wear_the_ambiguous_marker(outcome):
    verdict = evaluate_outcome(_rate_limited(outcome))

    assert marker_for_verdict(verdict, Priority.HIGH) != "🟡"
# --- boot_summary: the event IS the trouble (2026-08-20) --------------------
# laptop-start.ps1 Get-BootSummaryPayload gates the emit:
#   if (state -ne 'failed' -and failedSteps.Count -eq 0 -and errAnoms.Count -eq 0)
#       { return $null }
# so this event EXISTS only when the boot had trouble -- it is monodirectional
# at the type level, and DEGRADED is its floor.
#
# `state` is NOT the severity signal and must not be read as one: it reports
# whether the boot SEQUENCE completed, not whether it was healthy. The real
# 2026-08-19 event carries state="done" alongside 66 error anomalies. Treating
# state=="done" as success -- the obvious reading, and the one first proposed
# here -- would render that boot green. Severity comes from the FAILED COUNT.


def _boot(state, failed, anomalies=0, skipped=0):
    return _event(
        {"boot_id": "20260819-044507", "state": state, "total": 25,
         "done": 25 - failed, "failed": failed, "skipped": skipped,
         "failures": ["[critical] step: detail"] * failed,
         "anomalies": ["kind: detail"] * anomalies},
        event_type=EventType.BOOT_SUMMARY,
        priority=Priority.HIGH,
    )


def test_boot_summary_with_a_failed_step_is_failed():
    """Real 2026-08-10 and 2026-08-11 events: state=failed, failed=1."""
    verdict = evaluate_outcome(_boot("failed", failed=1))

    assert verdict.state is OutcomeState.FAILED


def test_boot_summary_with_only_anomalies_is_degraded_not_failed():
    """Real 2026-08-19 event: state="done", failed=0, 66 error anomalies. The
    boot sequence finished; error-severity anomalies are why it alerted at
    all. Degraded, not a hard failure."""
    verdict = evaluate_outcome(_boot("done", failed=0, anomalies=66, skipped=1))

    assert verdict.state is OutcomeState.DEGRADED


def test_boot_summary_done_is_never_success():
    """THE TRAP. state="done" means the sequence completed, NOT that the boot
    was clean -- the producer would not have emitted at all if it were. A
    _SUCCESS_VALUES reading of "done" would paint a 66-anomaly boot green.
    """
    for anomalies in (1, 2, 66):
        verdict = evaluate_outcome(_boot("done", failed=0, anomalies=anomalies))

        assert verdict.state is not OutcomeState.SUCCEEDED
        assert verdict.state is not OutcomeState.RECOVERED
        assert marker_for_verdict(verdict, Priority.HIGH) != "🟢"


def test_boot_summary_failed_count_beats_a_done_state():
    """Failure wins: a boot whose sequence says "done" but which lost a step
    is FAILED, not DEGRADED."""
    verdict = evaluate_outcome(_boot("done", failed=2, anomalies=3))

    assert verdict.state is OutcomeState.FAILED


def test_boot_summary_never_wears_the_ambiguous_marker():
    for state, failed in (("failed", 1), ("done", 0)):
        verdict = evaluate_outcome(_boot(state, failed=failed, anomalies=1))

        assert marker_for_verdict(verdict, Priority.HIGH) != "🟡"
