"""Routing pins for individual event types (v3: via events.routing_policy)."""

from events.routing_policy import (
    ACTION_REQUIRED,
    ALERTS,
    SECURITY,
    classify,
)
from events.schema import Event, EventType


def _route(et, payload=None, source="test"):
    return classify(Event.create(et, source, payload or {}))


def test_r57_events_route_to_their_topics():
    # backend_contract_drift moved to security_and_system (2026-05-30, operator
    # pref); agent_loop_fault stays on the alerts topic.
    assert _route(EventType.BACKEND_CONTRACT_DRIFT).topic_key == SECURITY
    assert _route(EventType.AGENT_LOOP_FAULT).topic_key == ALERTS


def test_resource_pressure_routes_to_watchdog_alerts():
    # Resource-pressure early-warning (2026-06-11 pagefile-burst remediation)
    # is an operator system-health signal — same topic as gateway_health so a
    # sudden commit/disk/pagefile alert lands in the stream the operator
    # already watches for infrastructure trouble.
    assert _route(EventType.RESOURCE_PRESSURE).topic_key == ALERTS


def test_tracker_partial_backlog_routes_to_action_required():
    # The partial-backlog alert (2026-07-14) is a human-action signal: an
    # operator must re-drive or investigate a growing partial/ queue — v3
    # sends ALL human-action signals to the cross-domain action_required
    # topic (which aliases onto the old jobflow_decisions thread).
    assert _route(EventType.TRACKER_PARTIAL_BACKLOG).topic_key == ACTION_REQUIRED
