"""Tests for events.producers.health_monitor — GatewayHealthMonitor."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.health_monitor import GatewayHealthMonitor


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


class TestHealthMonitor:
    def test_emits_on_state_change_down(self, bus):
        monitor = GatewayHealthMonitor(bus)

        # Initially unknown -> transition to down
        monitor.report_health("whatsapp", healthy=False, detail="Bridge unreachable")

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 1
        assert events[0].payload["platform"] == "whatsapp"
        assert events[0].payload["status"] == "down"
        assert events[0].payload["detail"] == "Bridge unreachable"

    def test_no_event_on_same_state(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("telegram", healthy=True)
        monitor.report_health("telegram", healthy=True)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        # First report transitions from unknown->up, second is same state
        assert len(events) == 1

    def test_emits_on_recovery(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("whatsapp", healthy=False)
        monitor.report_health("whatsapp", healthy=True)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 2
        assert events[0].payload["status"] == "down"
        assert events[1].payload["status"] == "up"

    def test_tracks_platforms_independently(self, bus):
        monitor = GatewayHealthMonitor(bus)

        monitor.report_health("whatsapp", healthy=True)
        monitor.report_health("telegram", healthy=False)

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 2
