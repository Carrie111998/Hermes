def test_r57_events_route_to_their_topics():
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    # backend_contract_drift moved to security_and_system (2026-05-30, operator pref);
    # agent_loop_fault stays on watchdog_alerts.
    assert TOPIC_ROUTING["backend_contract_drift"] == "security_and_system"
    assert TOPIC_ROUTING["agent_loop_fault"] == "watchdog_alerts"


def test_resource_pressure_routes_to_watchdog_alerts():
    # Resource-pressure early-warning (2026-06-11 pagefile-burst remediation)
    # is an operator system-health signal — same topic as gateway_health and
    # the watchdog signals so a sudden commit/disk/pagefile alert lands in the
    # stream the operator already watches for infrastructure trouble.
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    assert TOPIC_ROUTING["resource_pressure"] == "watchdog_alerts"


def test_tracker_partial_backlog_routes_to_jobflow_decisions():
    # The partial-backlog alert (2026-07-14) is a human-action signal: an
    # operator must re-drive or investigate a growing partial/ queue — same lane
    # as approvals/apply-packets.
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    assert TOPIC_ROUTING["tracker_partial_backlog"] == "jobflow_decisions"
