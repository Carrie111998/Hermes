"""Tests for events.subscribers.base -- BaseSubscriber and SubscriberRegistry."""

import time

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.subscribers.base import BaseSubscriber, SubscriberRegistry


def _seed_subscribers(bus: EventBus, *subscribers: BaseSubscriber) -> None:
    """Force each subscriber's cursor to 0 so poll() returns events emitted
    BEFORE the first poll. The bus's first-registration default (bus.py
    subscribe(), 2026-04-28) jumps to head-of-bus to prevent backlog floods
    on real deploys; tests that emit before polling need explicit backfill.
    Call AFTER any mutation of subscriber.subscriber_id so the right key is
    seeded."""
    for s in subscribers:
        bus._execute(
            """INSERT INTO subscriber_cursors (subscriber_id, last_rowid, updated_at)
               VALUES (?, 0, datetime('now'))
               ON CONFLICT(subscriber_id) DO UPDATE SET last_rowid = 0""",
            (s.subscriber_id,),
        )


class FlakySubscriber(BaseSubscriber):
    """Subscriber whose handle() can be made to throw on demand."""

    subscriber_id = "flaky"
    poll_interval_seconds = 1
    # Tighten the breaker so tests don't need real cooldowns
    CIRCUIT_BREAKER_ERROR_THRESHOLD = 3
    CIRCUIT_BREAKER_COOLDOWN_SECONDS = 1

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        self.raise_error = False
        self.handle_calls = 0

    def handle(self, event):
        self.handle_calls += 1
        if self.raise_error:
            raise RuntimeError("boom")


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
        _seed_subscribers(bus, sub)

        bus.emit(EventType.CRON_COMPLETED, "scout", {"a": 1})
        bus.emit(EventType.CRON_STARTED, "matcher", {"b": 2})

        processed = sub.poll()
        assert processed == 2
        assert len(sub.processed) == 2

    def test_poll_advances_cursor(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)
        _seed_subscribers(bus, sub)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        sub.poll()
        assert len(sub.processed) == 1

        bus.emit(EventType.CRON_COMPLETED, "matcher", {})
        sub.poll()
        assert len(sub.processed) == 2  # only the new one

    def test_filtered_subscriber(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FilteredSubscriber(bus)
        _seed_subscribers(bus, sub)

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
        _seed_subscribers(bus, sub)
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
        _seed_subscribers(bus, sub1, sub2)

        registry.register(sub1)
        registry.register(sub2)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        results = registry.poll_all()
        assert results == {"stub-1": 1, "stub-2": 1}


class TestCircuitBreaker:
    """After N consecutive handle() errors, a subscriber is quarantined for a cooldown."""

    def test_consecutive_errors_counter_increments(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True
        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()
        assert sub._consecutive_errors == 1

    def test_success_resets_consecutive_errors(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)

        # First event fails
        sub.raise_error = True
        bus.emit(EventType.CRON_COMPLETED, "test", {"i": 1})
        sub.poll()
        assert sub._consecutive_errors == 1

        # Next event succeeds
        sub.raise_error = False
        bus.emit(EventType.CRON_COMPLETED, "test", {"i": 2})
        sub.poll()
        assert sub._consecutive_errors == 0

    def test_threshold_opens_circuit(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        # Emit exactly threshold events — after processing, circuit opens
        for _ in range(sub.CIRCUIT_BREAKER_ERROR_THRESHOLD):
            bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()
        assert sub._is_quarantined()

    def test_quarantined_subscriber_skips_handle(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        # Trip the breaker
        for _ in range(sub.CIRCUIT_BREAKER_ERROR_THRESHOLD):
            bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()
        assert sub._is_quarantined()

        # Emit more events — they should not be handled while quarantined
        calls_before = sub.handle_calls
        for _ in range(5):
            bus.emit(EventType.CRON_STARTED, "test", {})
        result = sub.poll()
        assert result == 0
        assert sub.handle_calls == calls_before

    def test_cooldown_expiry_reopens_circuit(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        for _ in range(sub.CIRCUIT_BREAKER_ERROR_THRESHOLD):
            bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()
        assert sub._is_quarantined()

        # Fast-forward: pretend cooldown has elapsed
        sub._quarantined_until = time.monotonic() - 1

        # Now fix the handler and emit a new event
        sub.raise_error = False
        bus.emit(EventType.CRON_STARTED, "test", {})
        processed = sub.poll()
        # Cursor may include previously-queued events too; what matters is
        # that we left quarantine and successfully processed something.
        assert processed >= 1
        assert not sub._is_quarantined()
        assert sub._consecutive_errors == 0

    def test_emits_agent_error_on_trip(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        for _ in range(sub.CIRCUIT_BREAKER_ERROR_THRESHOLD):
            bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 1
        assert errors[0].payload.get("subscriber_id") == "flaky"
        assert "quarantined" in errors[0].payload.get("error", "").lower()

    def test_agent_error_not_duplicated_while_quarantined(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        # Trip
        for _ in range(sub.CIRCUIT_BREAKER_ERROR_THRESHOLD):
            bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        # More polls while quarantined shouldn't produce more agent_errors
        for _ in range(3):
            sub.poll()
        errors = bus.query(event_type=EventType.AGENT_ERROR)
        assert len(errors) == 1


class TestLagAlertHelper:
    """_check_subscriber_lag emits agent_error events when subscribers fall behind."""

    def _setup(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()
        slow = StubSubscriber(bus)
        slow.subscriber_id = "slow"
        fast = StubSubscriber(bus)
        fast.subscriber_id = "fast"
        _seed_subscribers(bus, slow, fast)
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
        _seed_subscribers(bus, a, b)
        registry.register(a)
        registry.register(b)

        bus.emit(EventType.CRON_COMPLETED, "scout", {})
        bus.emit(EventType.CRON_COMPLETED, "scout", {})

        # sub-a processes, sub-b doesn't
        a.poll()

        report = registry.lag_report()
        assert report == {"sub-a": 0, "sub-b": 2}

    def test_lag_report_respects_subscriber_filters(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        registry = SubscriberRegistry()

        filtered = FilteredSubscriber(bus)
        _seed_subscribers(bus, filtered)
        registry.register(filtered)

        for _ in range(150):
            bus.emit(EventType.CRON_STARTED, "scout", {})

        assert registry.lag_report() == {"filtered": 0}

        bus.emit(EventType.JOB_HIGH_SCORE, "matcher", {}, priority=Priority.HIGH)

        assert registry.lag_report() == {"filtered": 1}


class TestDeadLetterRecording:
    """SR-109: failed handler invocations must leave a dead-letter row."""

    def test_handler_exception_records_dead_letter(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        _seed_subscribers(bus, sub)
        sub.raise_error = True

        eid = bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        rows = bus.get_dead_letters()
        assert len(rows) == 1
        assert rows[0]["event_id"] == eid
        assert rows[0]["subscriber_id"] == "flaky"
        assert "RuntimeError" in rows[0]["error"]
        assert "boom" in rows[0]["error"]

    def test_successful_handler_records_no_dead_letter(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        assert bus.get_dead_letters() == []

    def test_repeat_failure_increments_attempts(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        sub.raise_error = True

        # Two events both fail in separate polls so the cursor advances each time.
        bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()
        # Same event cannot be re-polled (cursor advanced past it), but manual
        # re-insertion simulates a second subscriber failure on the same event_id.
        eid = bus.emit(EventType.CRON_COMPLETED, "test", {"retry": True})
        bus.record_dead_letter("flaky", eid, "boom")
        bus.record_dead_letter("flaky", eid, "boom again")

        rows = bus.get_dead_letters(subscriber_id="flaky")
        # There should be one entry per (event_id, subscriber) pair.
        by_event = {r["event_id"]: r for r in rows}
        assert by_event[eid]["attempts"] == 2
        assert by_event[eid]["error"] == "boom again"

    def test_dead_letter_recording_is_safe_when_bus_fails(self, tmp_path, monkeypatch):
        """A bus-level failure in record_dead_letter must not cascade into the subscriber."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        sub.raise_error = True

        def boom(*args, **kwargs):
            raise RuntimeError("dead-letter table is broken")

        monkeypatch.setattr(bus, "record_dead_letter", boom)

        bus.emit(EventType.CRON_COMPLETED, "test", {})
        # Poll must not raise even though both handle() and record_dead_letter() fail.
        sub.poll()


class TestAtLeastOnceDedup:
    """SR-101: handled_events redelivery check — subscribers must not double-fire."""

    def test_successful_handle_marks_handled(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)
        _seed_subscribers(bus, sub)

        eid = bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        assert bus.is_handled("stub", eid)

    def test_already_handled_event_skips_handle(self, tmp_path):
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)

        eid = bus.emit(EventType.CRON_COMPLETED, "test", {})
        # Simulate an earlier successful handle (e.g., from before a crash).
        bus.mark_handled("stub", eid)

        sub.poll()
        # handle() was NOT called this time — the event was skipped as redelivery.
        assert sub.processed == []
        # But the cursor must still advance (otherwise we'd re-fetch forever).
        assert sub.poll() == 0

    def test_failed_handle_does_not_mark_handled(self, tmp_path):
        """A handler that raises must leave no handled_events row so operators can retry."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = FlakySubscriber(bus)
        sub.raise_error = True

        eid = bus.emit(EventType.CRON_COMPLETED, "test", {})
        sub.poll()

        assert bus.is_handled("flaky", eid) is False

    def test_mixed_batch_marks_only_successes(self, tmp_path):
        """Batch with one failure: only the good event gets a handled_events row."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")

        class SelectiveFailure(BaseSubscriber):
            subscriber_id = "selective"
            poll_interval_seconds = 1
            CIRCUIT_BREAKER_ERROR_THRESHOLD = 99  # never trip

            def __init__(self, bus):
                super().__init__(bus)
                self.processed = []

            def handle(self, event):
                if event.payload.get("bad"):
                    raise RuntimeError("no")
                self.processed.append(event)

        sub = SelectiveFailure(bus)
        _seed_subscribers(bus, sub)
        good = bus.emit(EventType.CRON_COMPLETED, "test", {"bad": False})
        bad = bus.emit(EventType.CRON_COMPLETED, "test", {"bad": True})

        sub.poll()

        assert bus.is_handled("selective", good) is True
        assert bus.is_handled("selective", bad) is False
        assert len(sub.processed) == 1

    def test_mark_handled_failure_does_not_cascade(self, tmp_path, monkeypatch):
        """A bus-level failure in mark_handled must not cascade into the subscriber."""
        bus = EventBus(db_path=tmp_path / "events" / "test.db")
        sub = StubSubscriber(bus)
        _seed_subscribers(bus, sub)

        def boom(*args, **kwargs):
            raise RuntimeError("handled_events table broken")

        monkeypatch.setattr(bus, "mark_handled", boom)

        bus.emit(EventType.CRON_COMPLETED, "test", {})
        # Poll must complete cleanly — handle() ran, mark_handled() failed, subscriber continues.
        count = sub.poll()
        assert count == 1
        assert len(sub.processed) == 1
