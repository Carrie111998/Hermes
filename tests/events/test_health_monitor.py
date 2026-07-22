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


class TestWhatsAppProbeDebounce:
    """A single failed /health probe must not emit a down event.

    The bridge's node event loop can stall past the 5s probe timeout for
    one 60s cycle during a Baileys resync; alerting on every such blip
    produced dozens of down->up notification pairs per day (2026-07-18 RCA).
    """

    def _make_probe_fail(self, monkeypatch):
        import requests

        def boom(*args, **kwargs):
            raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=5)")

        monkeypatch.setattr(requests, "get", boom)

    def _make_probe_succeed(self, monkeypatch):
        import requests

        class _Resp:
            status_code = 200

        monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())

    def test_single_probe_failure_suppressed(self, bus, monkeypatch):
        monitor = GatewayHealthMonitor(bus)
        monitor._last_state["whatsapp"] = True

        self._make_probe_fail(monkeypatch)
        monitor._check_whatsapp()

        assert bus.query(event_type=EventType.GATEWAY_HEALTH) == []

    def test_consecutive_probe_failures_emit_down(self, bus, monkeypatch):
        monitor = GatewayHealthMonitor(bus)
        monitor._last_state["whatsapp"] = True

        self._make_probe_fail(monkeypatch)
        monitor._check_whatsapp()
        monitor._check_whatsapp()

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert len(events) == 1
        assert events[0].payload["status"] == "down"

    def test_success_resets_failure_streak(self, bus, monkeypatch):
        monitor = GatewayHealthMonitor(bus)
        monitor._last_state["whatsapp"] = True

        self._make_probe_fail(monkeypatch)
        monitor._check_whatsapp()  # blip 1 — suppressed
        self._make_probe_succeed(monkeypatch)
        monitor._check_whatsapp()  # recovered — streak reset, still up
        self._make_probe_fail(monkeypatch)
        monitor._check_whatsapp()  # blip 2 after reset — suppressed again

        assert bus.query(event_type=EventType.GATEWAY_HEALTH) == []

    def test_recovery_after_down_emits_up_immediately(self, bus, monkeypatch):
        monitor = GatewayHealthMonitor(bus)
        monitor._last_state["whatsapp"] = True

        self._make_probe_fail(monkeypatch)
        monitor._check_whatsapp()
        monitor._check_whatsapp()  # down emitted
        self._make_probe_succeed(monkeypatch)
        monitor._check_whatsapp()  # up must not be debounced

        events = bus.query(event_type=EventType.GATEWAY_HEALTH)
        assert [e.payload["status"] for e in events] == ["down", "up"]
