"""The P6 audit pair must integrate cleanly with the event system: covered by
coverage.py, routed as trace-level security telemetry that never pages, and a
failed result must promote to WARN via the generic verdict path (no hook)."""

from events.outcomes import OutcomeState, evaluate_outcome
from events.routing_policy import SECURITY, Attention, classify
from events.schema import Event, EventType


def _event(et, payload=None):
    return Event.create(event_type=et, source="claude-fleet-controller",
                        payload=payload or {})


def test_new_types_exist_and_round_trip():
    assert EventType.from_string("claude_fleet_plan") is EventType.CLAUDE_FLEET_PLAN
    assert EventType.from_string("claude_fleet_result") is EventType.CLAUDE_FLEET_RESULT
    assert EventType.CLAUDE_FLEET_PLAN.icon.strip()
    assert EventType.CLAUDE_FLEET_RESULT.icon.strip()


def test_coverage_manifest_is_total():
    """Adding an EventType without a routing entry is the drift this catches."""
    from events.coverage import coverage_gaps

    assert coverage_gaps() == {}, coverage_gaps()


def test_routine_shadow_events_are_trace_and_never_page():
    for et in (EventType.CLAUDE_FLEET_PLAN, EventType.CLAUDE_FLEET_RESULT):
        route = classify(_event(et, {"decision": "shadow_projected"}))
        assert route.attention is Attention.TRACE
        assert route.topic_key == SECURITY
        assert route.wa_tier is None
        assert route.batch is True


def test_no_action_result_stays_trace():
    route = classify(_event(EventType.CLAUDE_FLEET_RESULT, {"status": "no_action"}))
    assert route.attention is Attention.TRACE
    assert route.wa_tier is None


def test_failed_result_promotes_to_warn_via_generic_verdict_path():
    """status=='failed' is scanned by events.outcomes -> FAILED verdict, and
    the generic TRACE->WARN promotion (not a fleet-specific hook) lifts it to
    the alerts-grade security lane so a broken enforcement pass is visible."""
    ev = _event(EventType.CLAUDE_FLEET_RESULT, {"status": "failed",
                                                "detail": "taskkill failed"})
    assert evaluate_outcome(ev).state is OutcomeState.FAILED
    route = classify(ev)
    assert route.attention is Attention.WARN
    # WARN on the security topic still does not page for a NORMAL/HIGH priority.
    assert route.wa_tier is None


def test_hard_terminated_result_is_not_a_failure():
    ev = _event(EventType.CLAUDE_FLEET_RESULT, {"status": "hard_terminated"})
    # 'hard_terminated' is not in the failure value set, so it stays trace.
    route = classify(ev)
    assert route.attention is Attention.TRACE
