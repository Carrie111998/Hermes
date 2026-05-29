def test_r57_events_route_to_watchdog_alerts():
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    assert TOPIC_ROUTING["backend_contract_drift"] == "watchdog_alerts"
    assert TOPIC_ROUTING["agent_loop_fault"] == "watchdog_alerts"
