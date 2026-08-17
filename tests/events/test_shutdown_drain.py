"""The EventBus shutdown drain — makes shutdown-time delivery deterministic.

``gateway/run.py`` emits GATEWAY_STOPPED early in ``_stop_impl_body`` (:9535)
and calls ``events.gateway_integration.shutdown()`` late in ``main()``'s
teardown (:23881). Between those two points the only thing that could deliver
the event to a subscriber was the ordinary poll loop happening to tick — and
``CronStaleMonitor.poll_interval_seconds`` is 60 against a teardown window
measured at ~60s. So ``CronStaleMonitor._resolve_gateway_stopped`` (the
2026-08-16 shutdown-attribution feature) fired on a coin flip.

The successor process cannot cover the miss by HANDLING the event:
``_started_event_ids`` is per-process in-memory state, and ``BaseSubscriber``
seeds its cursor with INSERT OR IGNORE, so a restart PRESERVES the cursor and
never replays the CRON_STARTED that built that map. Verified in production
2026-08-17: the new process handled the GATEWAY_STOPPED at 04:12:03 and emitted
nothing.

``shutdown()`` therefore drains subscribers itself, after the poll threads are
joined (so nothing polls concurrently) and before the bus is closed.

That covers a GRACEFUL teardown. A force-killed one reaches neither the drain
nor ``shutdown_all()``, so ``CronStaleMonitor.startup()`` rebuilds the
attribution from the bus in the SUCCESSOR — not by handling the event, but by
querying for it. The last two tests here pin the two paths composing to exactly
one record.
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


def test_shutdown_calls_subscriber_shutdown_after_the_drain_and_before_close(bus, monkeypatch):
    """CronStaleMonitor stages its shutdown attribution on poll() and emits it
    in shutdown() — the last moment before the process exits. That only works
    if the drain runs FIRST (so the GATEWAY_STOPPED has been seen) and the bus
    is still open when shutdown() emits."""
    trace = []

    class Lifecycle(FakeSubscriber):
        def poll(self):
            trace.append(("poll", gi._bus is not None))
            return super().poll()

        def shutdown(self):
            trace.append(("shutdown", gi._bus is not None))

    sub = Lifecycle("cron-stale-monitor", [EventType.GATEWAY_STOPPED])
    monkeypatch.setattr(gi, "_registry", _registry(sub))
    monkeypatch.setattr(gi, "_bus", bus)
    monkeypatch.setattr(gi, "_subscriber_thread", None)
    monkeypatch.setattr(gi, "_applier_thread", None)

    gi.shutdown()

    assert [step for step, _ in trace] == ["poll", "shutdown"]
    assert all(bus_open for _, bus_open in trace), "bus closed before shutdown()"


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

def _wire_gateway(monkeypatch, bus, *subs):
    monkeypatch.setattr(gi, "_registry", _registry(*subs))
    monkeypatch.setattr(gi, "_bus", bus)
    monkeypatch.setattr(gi, "_subscriber_thread", None)
    monkeypatch.setattr(gi, "_applier_thread", None)


def test_cron_stale_monitor_attributes_the_killed_run_during_teardown(bus, monkeypatch):
    """Production shape, through the real shutdown() sequence: a cron is in
    flight, the gateway stops, and the attribution lands before this process
    dies — not on a 60s coin flip.

    Both halves matter and neither works alone: the DRAIN is what makes the
    monitor see the GATEWAY_STOPPED at all, and shutdown_all() is what flushes
    the staged report at the last moment.
    """
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

    _wire_gateway(monkeypatch, bus, monitor)
    gi.shutdown()

    stale = [e for e in bus.query() if e.event_type == EventType.CRON_STALE]
    assert len(stale) == 1, "the shutdown-killed run was not attributed"
    assert stale[0].payload["scope"] == "gateway_stopped"
    assert stale[0].payload["job_id"] == "1f8c450136b5"
    assert stale[0].payload["exit_reason"] == "restart"
    assert stale[0].payload["cron_started_event_id"] == started_id
    assert stale[0].priority == Priority.NORMAL


def test_a_run_that_lands_during_teardown_is_not_reported_killed(bus, monkeypatch):
    """The 2026-08-17 false positive, at the integration level.

    gateway/run.py snapshots the in-flight ids EARLY in its stop path, so a run
    that is merely unfinished at that instant can still complete while the
    gateway tears down — jobflow-researcher did, 35s after being reported
    killed. The drain must deliver BOTH events before anything is emitted.
    """
    monitor = CronStaleMonitor(bus)

    started_id = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="jobflow-researcher",
        payload={"job_id": "785bd746431b", "job_name": "jobflow-researcher"},
    )
    monitor.poll()

    bus.emit(
        event_type=EventType.GATEWAY_STOPPED,
        source="gateway",
        payload={
            "exit_reason": "graceful",
            "inflight_cron_correlation_ids": [started_id],
        },
    )
    # ...and the run lands anyway, while the gateway is still tearing down.
    bus.emit(
        event_type=EventType.CRON_COMPLETED,
        source="jobflow-researcher",
        payload={"job_id": "785bd746431b", "job_name": "jobflow-researcher"},
    )

    _wire_gateway(monkeypatch, bus, monitor)
    gi.shutdown()

    stale = [e for e in bus.query() if e.event_type == EventType.CRON_STALE]
    assert stale == [], (
        "a run that completed during teardown was reported shutdown-killed"
    )


# ---------------------------------------------------------------------------
# The hard-kill path: neither the drain nor shutdown_all() ever runs
# ---------------------------------------------------------------------------

def test_a_force_killed_teardown_is_attributed_by_the_next_gateway(bus):
    """No drain, no shutdown_all() — the predecessor simply stops existing.

    GATEWAY_STOPPED survives because gateway/run.py emits it EARLY, which is
    exactly why the snapshot has to stay there. Everything the predecessor
    staged does not survive. The successor reconstructs from what did, through
    the real registry call site (startup_all()).
    """
    predecessor = CronStaleMonitor(bus)
    started_id = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="jobflow-researcher",
        payload={"job_id": "785bd746431b", "job_name": "jobflow-researcher"},
    )
    predecessor.poll()
    bus.emit(
        event_type=EventType.GATEWAY_STOPPED,
        source="gateway",
        payload={
            "exit_reason": "force_kill",
            "inflight_cron_correlation_ids": [started_id],
        },
    )
    predecessor.poll()  # stages the report — and then taskkill /F lands
    assert predecessor._pending_shutdown, "premise: a report was staged"
    assert [e for e in bus.query() if e.event_type == EventType.CRON_STALE] == []

    _registry(CronStaleMonitor(bus)).startup_all()

    stale = [e for e in bus.query() if e.event_type == EventType.CRON_STALE]
    assert len(stale) == 1, "the force-killed run was never attributed"
    assert stale[0].payload["job_id"] == "785bd746431b"
    assert stale[0].payload["exit_reason"] == "force_kill"
    assert stale[0].payload["cron_started_event_id"] == started_id
    assert stale[0].priority == Priority.NORMAL


def test_a_graceful_teardown_is_not_reported_twice_by_the_successor(bus, monkeypatch, caplog):
    """The two paths compose. The predecessor's flush already wrote the record,
    so the successor's startup must find it and stay quiet — the dedupe is a bus
    query on (gateway_stopped_event_id, cron_started_event_id), which is why no
    new state is needed to make this safe.

    The log assertion is what keeps this honest: a count of 1 is also what you
    get from a successor that does nothing at all, so the test must show the
    pass ran, saw the shutdown, and suppressed itself on purpose.
    """
    predecessor = CronStaleMonitor(bus)
    started_id = bus.emit(
        event_type=EventType.CRON_STARTED,
        source="jobflow-tracker-cycle",
        payload={"job_id": "1f8c450136b5", "job_name": "jobflow-tracker-cycle"},
    )
    predecessor.poll()
    bus.emit(
        event_type=EventType.GATEWAY_STOPPED,
        source="gateway",
        payload={
            "exit_reason": "restart",
            "inflight_cron_correlation_ids": [started_id],
        },
    )

    _wire_gateway(monkeypatch, bus, predecessor)
    gi.shutdown()
    assert len([e for e in bus.query() if e.event_type == EventType.CRON_STALE]) == 1

    with caplog.at_level(logging.DEBUG,
                         logger="events.subscribers.cron_stale_monitor"):
        _registry(CronStaleMonitor(bus)).startup_all()

    stale = [e for e in bus.query() if e.event_type == EventType.CRON_STALE]
    assert len(stale) == 1, (
        f"the successor re-reported the predecessor's record: "
        f"{[e.payload for e in stale]}"
    )
    messages = [r.getMessage() for r in caplog.records]
    assert any("already reported" in m for m in messages), (
        f"the successor never examined the shutdown, so the count of 1 proves "
        f"nothing about deduping: {messages}"
    )
