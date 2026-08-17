"""The EventBus shutdown drain — makes shutdown-time delivery deterministic.

``gateway/run.py`` emits GATEWAY_STOPPED early in ``_stop_impl_body`` (:9535)
and calls ``events.gateway_integration.shutdown()`` late in ``main()``'s
teardown (:23881). Between those two points the only thing that could deliver
the event to a subscriber was the ordinary poll loop happening to tick — and
``CronStaleMonitor.poll_interval_seconds`` is 60 against a teardown window
measured at ~60s. So ``CronStaleMonitor._resolve_gateway_stopped`` (the
2026-08-16 shutdown-attribution feature) fired on a coin flip.

The successor process cannot cover the miss: ``_started_event_ids`` is
per-process in-memory state, and ``BaseSubscriber`` seeds its cursor with
INSERT OR IGNORE, so a restart PRESERVES the cursor and never replays the
CRON_STARTED that built that map. Verified in production 2026-08-17: the
new process handled the GATEWAY_STOPPED at 04:12:03 and emitted nothing.

``shutdown()`` therefore drains subscribers itself, after the poll threads are
joined (so nothing polls concurrently) and before the bus is closed.
"""
import logging
import time

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.subscribers.base import SubscriberRegistry
from events.subscribers.cron_stale_monitor import CronStaleMonitor

import events.gateway_integration as gi


@pytest.fixture
def bus(tmp_path):
    b = EventBus(db_path=tmp_path / "event_bus.db")
    yield b
    b.close()


class FakeSubscriber:
    """Minimal stand-in: the drain only needs id, event_types and poll()."""

    def __init__(self, subscriber_id, event_types=None, sleep=0.0, boom=False):
        self.subscriber_id = subscriber_id
        self.event_types = event_types
        self.polls = 0
        self._sleep = sleep
        self._boom = boom

    def poll(self):
        self.polls += 1
        if self._sleep:
            time.sleep(self._sleep)
        if self._boom:
            raise RuntimeError("subscriber exploded")
        return 0


def _registry(*subs):
    reg = SubscriberRegistry()
    for sub in subs:
        reg.register(sub)
    return reg


# ---------------------------------------------------------------------------
# The drain itself
# ---------------------------------------------------------------------------

def test_drain_polls_subscribers_so_a_shutdown_event_is_delivered_before_exit():
    sub = FakeSubscriber("cron-stale-monitor", [EventType.GATEWAY_STOPPED])

    gi._drain_subscribers_for_shutdown(_registry(sub))

    assert sub.polls == 1


def test_drain_polls_gateway_stopped_consumers_before_everyone_else():
    """Order is the guarantee: the deadline below may cut the tail short, so
    the subscribers that exist to see the shutdown must already have run."""
    order = []

    class Recording(FakeSubscriber):
        def poll(self):
            order.append(self.subscriber_id)
            return super().poll()

    unrelated = Recording("mailbox-translator", [EventType.CRON_STARTED])
    consumer = Recording("cron-stale-monitor", [EventType.GATEWAY_STOPPED])
    catch_all = Recording("audit-logger", None)

    gi._drain_subscribers_for_shutdown(_registry(unrelated, consumer, catch_all))

    assert order.index("cron-stale-monitor") < order.index("mailbox-translator")
    # event_types=None means "every event", so audit-logger sees the shutdown
    # event too and belongs in the same privileged group.
    assert order.index("audit-logger") < order.index("mailbox-translator")


def test_drain_polls_gateway_stopped_consumers_even_with_no_time_budget_left():
    """The deadline bounds the best-effort tail, never the guarantee."""
    consumer = FakeSubscriber("cron-stale-monitor", [EventType.GATEWAY_STOPPED])
    unrelated = FakeSubscriber("mailbox-translator", [EventType.CRON_STARTED])

    gi._drain_subscribers_for_shutdown(
        _registry(consumer, unrelated), timeout_seconds=0.0
    )

    assert consumer.polls == 1, "the shutdown consumer must always be drained"
    assert unrelated.polls == 0, "the tail must yield to the deadline"


def test_drain_stops_the_tail_at_the_deadline_and_says_what_it_skipped(caplog):
    """Teardown creeping toward the 30s taskkill cap is what leaves the gateway
    DOWN on this box, so the drain must be bounded — and must not do it
    silently."""
    slow = FakeSubscriber("digest-composer", [EventType.CRON_STARTED], sleep=0.2)
    never = FakeSubscriber("critic-trigger", [EventType.CRON_STARTED])

    with caplog.at_level(logging.WARNING, logger=gi.logger.name):
        gi._drain_subscribers_for_shutdown(
            _registry(slow, never), timeout_seconds=0.05
        )

    assert slow.polls == 1
    assert never.polls == 0
    assert "critic-trigger" in caplog.text


def test_drain_skips_the_subscriber_that_has_its_own_thread():
    """IntentApplier is single-threaded by design and _subscriber_poll_loop
    already excludes it; a drain racing its 5s join would be a second caller."""
    applier = FakeSubscriber("tracker-intent-applier", None)
    other = FakeSubscriber("cron-stale-monitor", [EventType.GATEWAY_STOPPED])

    gi._drain_subscribers_for_shutdown(_registry(applier, other), skip=(applier,))

    assert applier.polls == 0
    assert other.polls == 1


def test_drain_survives_a_subscriber_whose_poll_raises():
    boom = FakeSubscriber("boom", [EventType.GATEWAY_STOPPED], boom=True)
    after = FakeSubscriber("cron-stale-monitor", [EventType.GATEWAY_STOPPED])

    gi._drain_subscribers_for_shutdown(_registry(boom, after))

    assert after.polls == 1, "one bad subscriber must not strand the rest"


# ---------------------------------------------------------------------------
# Wiring into shutdown()
# ---------------------------------------------------------------------------

def test_shutdown_drains_while_the_bus_is_still_open(bus, monkeypatch):
    """Order inside shutdown(): join threads -> drain -> shutdown_all -> close.
    Draining after close would poll a dead connection."""
    seen = {}

    class BusWatcher(FakeSubscriber):
        def poll(self):
            seen["bus_open"] = gi._bus is not None
            return super().poll()

    watcher = BusWatcher("cron-stale-monitor", [EventType.GATEWAY_STOPPED])
    monkeypatch.setattr(gi, "_registry", _registry(watcher))
    monkeypatch.setattr(gi, "_bus", bus)
    monkeypatch.setattr(gi, "_subscriber_thread", None)
    monkeypatch.setattr(gi, "_applier_thread", None)

    gi.shutdown()

    assert watcher.polls == 1, "shutdown() did not drain"
    assert seen["bus_open"] is True


def test_shutdown_drains_only_after_the_poll_thread_is_joined(bus, monkeypatch):
    """Two threads polling one subscriber would race its in-memory maps."""
    observed = {}

    class ThreadWatcher(FakeSubscriber):
        def poll(self):
            observed["thread"] = gi._subscriber_thread
            return super().poll()

    class FakeThread:
        def __init__(self):
            self.joined = False

        def join(self, timeout=None):
            self.joined = True

    watcher = ThreadWatcher("cron-stale-monitor", [EventType.GATEWAY_STOPPED])
    monkeypatch.setattr(gi, "_registry", _registry(watcher))
    monkeypatch.setattr(gi, "_bus", bus)
    monkeypatch.setattr(gi, "_subscriber_thread", FakeThread())
    monkeypatch.setattr(gi, "_applier_thread", None)

    gi.shutdown()

    assert observed["thread"] is None, "drained while the poll thread was live"


# ---------------------------------------------------------------------------
# End to end: the behaviour the drain exists for
# ---------------------------------------------------------------------------

def test_cron_stale_monitor_attributes_the_killed_run_during_the_drain(bus):
    """Production shape: a cron is in flight, the gateway stops, and the
    attribution lands before this process dies — not on a 60s coin flip."""
    monitor = CronStaleMonitor(bus)

    started_id = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="jobflow-tracker-cycle",
        payload={"job_id": "1f8c450136b5", "job_name": "jobflow-tracker-cycle"},
    )
    monitor.poll()  # builds _started_event_ids, as the live monitor had done

    bus.emit(
        event_type=EventType.GATEWAY_STOPPED,
        source="gateway",
        payload={
            "exit_reason": "restart",
            "inflight_cron_correlation_ids": [started_id],
        },
    )

    gi._drain_subscribers_for_shutdown(_registry(monitor))

    stale = [e for e in bus.query() if e.event_type == EventType.CRON_STALE]
    assert len(stale) == 1, "the shutdown-killed run was not attributed"
    assert stale[0].payload["scope"] == "gateway_stopped"
    assert stale[0].payload["job_id"] == "1f8c450136b5"
    assert stale[0].payload["exit_reason"] == "restart"
    assert stale[0].payload["cron_started_event_id"] == started_id
    assert stale[0].priority == Priority.NORMAL
