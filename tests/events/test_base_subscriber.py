"""Tests for events.subscribers.base -- BaseSubscriber and SubscriberRegistry."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.subscribers.base import BaseSubscriber, SubscriberRegistry


class StubSubscriber(BaseSubscriber):
    """Concrete subscriber for testing."""

    subscriber_id = "stub"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.processed = []

    def handle(self, event):
        self.processed.append(event)


class FilteredSubscriber(BaseSubscriber):
    subscriber_id = "filtered"
    poll_interval_seconds = 10
    event_types = [EventType.JOB_HIGH_SCORE, EventType.INTERVIEW_SIGNAL]
    min_priority = Priority.HIGH

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.processed = []

    def handle(self, event):
        self.processed.append(event)


class TestBaseSubscriber:
    def test_poll_processes_events(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        bus.emit(EventType.CRON_COMPLETED, "scout", {"a": 1})
        bus.emit(EventType.CRON_STARTED, "matcher", {"b": 2})

        processed = sub.poll()
        assert processed == 2
        assert len(sub.processed) == 2

    def test_poll_advances_cursor(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        sub.poll()
        assert len(sub.processed) == 1

        bus.emit(EventType.CRON_COMPLETED, "matcher", {})
        sub.poll()
        assert len(sub.processed) == 2  # only the new one

    def test_filtered_subscriber(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FilteredSubscriber(bus)

        bus.emit(EventType.CRON_STARTED, "scout", {})        # LOW -- filtered
        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {})     # HIGH -- passes
        bus.emit(EventType.CRON_COMPLETED, "scout", {})       # NORMAL -- filtered
        bus.emit(EventType.INTERVIEW_SIGNAL, "tracker", {})   # CRITICAL -- passes

        processed = sub.poll()
        assert processed == 2
        assert sub.processed[0].event_type == EventType.JOB_HIGH_SCORE
        assert sub.processed[1].event_type == EventType.INTERVIEW_SIGNAL

    def test_handle_error_does_not_stop_processing(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")

        class FailingSubscriber(BaseSubscriber):
            subscriber_id = "failing"
            poll_interval_seconds = 5

            def __init__(self, bus):
                super().__init__(bus)
                self.calls = 0

            def handle(self, event):
                self.calls += 1
                if self.calls == 1:
                    raise ValueError("boom")

        sub = FailingSubscriber(bus)
        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "matcher", {})

        processed = sub.poll()
        assert processed == 2  # both processed despite first error
        assert sub.calls == 2


class TestSubscriberRegistry:
    def test_register_and_list(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        sub = StubSubscriber(bus)
        registry.register(sub)

        assert len(registry.subscribers) == 1
        assert registry.subscribers[0] is sub

    def test_poll_all(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        sub1 = StubSubscriber(bus)
        sub1.subscriber_id = "stub-1"
        sub2 = StubSubscriber(bus)
        sub2.subscriber_id = "stub-2"

        registry.register(sub1)
        registry.register(sub2)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        results = registry.poll_all()
        assert results == {"stub-1": 1, "stub-2": 1}


class TestLagAlertHelper:
    """_check_subscriber_lag emits agent_error events when subscribers fall behind."""

    def _setup(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()
        slow = StubSubscriber(bus)
        slow.subscriber_id = "slow"
        fast = StubSubscriber(bus)
        fast.subscriber_id = "fast"
        registry.register(slow)
        registry.register(fast)
        return bus, registry, slow, fast

    def test_no_alert_when_all_under_threshold(self, tmp_path):
        from events.gateway_integration import _check_subscriber_lag
        bus, registry, slow, fast = self._setup(tmp_path)
        # Emit just a few events
        for _ in range(5):
            bus.emit(EventType.CRON_STARTED, "test", {})
        last_alerted = {}
        _check_subscriber_lag(registry, bus, threshold=100,
                              cooldown_seconds=900, last_alerted=last_alerted, now=1000.0)
        # No alerts should have fired
        assert last_alerted == {}
        # No agent_error events in the bus
        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 0

    def test_alert_emitted_when_lag_exceeds_threshold(self, tmp_path):
        from events.gateway_integration import _check_subscriber_lag
        bus, registry, slow, fast = self._setup(tmp_path)
        # Emit more events than threshold so both subscribers are behind
        for _ in range(150):
            bus.emit(EventType.CRON_STARTED, "test", {})
        # fast drains its cursor
        fast.poll()
        last_alerted = {}
        _check_subscriber_lag(registry, bus, threshold=100,
                              cooldown_seconds=900, last_alerted=last_alerted, now=1000.0)
        # slow should have an alert, fast shouldn't
        assert "slow" in last_alerted
        assert "fast" not in last_alerted
        # Exactly one agent_error event emitted
        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 1
        assert errors[0].payload["subscriber_id"] == "slow"
        assert errors[0].payload["lag"] > 100

    def test_cooldown_suppresses_repeat_alerts(self, tmp_path):
        from events.gateway_integration import _check_subscriber_lag
        bus, registry, slow, fast = self._setup(tmp_path)
        for _ in range(150):
            bus.emit(EventType.CRON_STARTED, "test", {})
        fast.poll()  # drain fast so only slow is a candidate
        last_alerted = {"slow": 950.0}  # alerted 50s ago
        _check_subscriber_lag(registry, bus, threshold=100,
                              cooldown_seconds=900, last_alerted=last_alerted, now=1000.0)
        # No new alert (still within 900s cooldown)
        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 0

    def test_cooldown_expired_allows_realert(self, tmp_path):
        from events.gateway_integration import _check_subscriber_lag
        bus, registry, slow, fast = self._setup(tmp_path)
        for _ in range(150):
            bus.emit(EventType.CRON_STARTED, "test", {})
        fast.poll()  # drain fast
        last_alerted = {"slow": 0.0}  # alerted long ago
        _check_subscriber_lag(registry, bus, threshold=100,
                              cooldown_seconds=900, last_alerted=last_alerted, now=10000.0)
        # Re-alert allowed
        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 1
        assert last_alerted["slow"] == 10000.0


class TestSubscriberLagReport:
    """Registry-level lag visibility: how far behind each subscriber is."""

    def test_lag_report_empty_registry(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()
        assert registry.lag_report() == {}

    def test_lag_report_per_subscriber(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        a = StubSubscriber(bus)
        a.subscriber_id = "sub-a"
        b = StubSubscriber(bus)
        b.subscriber_id = "sub-b"
        registry.register(a)
        registry.register(b)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        # sub-a processes, sub-b doesn't
        a.poll()

        report = registry.lag_report()
        assert report == {"sub-a": 0, "sub-b": 2}
