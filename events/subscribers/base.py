"""Base subscriber class and registry for the Hermes Event Bus.

Subscribers independently consume events from the bus via poll().
Each subscriber tracks its own cursor so multiple subscribers
can process the same events without interference.

Circuit breaker: if a subscriber's handle() raises CIRCUIT_BREAKER_ERROR_THRESHOLD
times in a row, the subscriber enters quarantine for CIRCUIT_BREAKER_COOLDOWN_SECONDS.
During quarantine, poll() is a no-op — events pile up in the backlog (visible via
subscriber_lag) instead of repeatedly invoking a broken handler.  A single
agent_error event is emitted on the transition into quarantine.

Cycle-prevention rule (2026-04-30): no subscriber that EMITS a delivery
event (NOTIFICATION_DELIVERED / NOTIFICATION_FAILED) may CONSUME a
delivery event. Today this applies to telegram-notifier and whatsapp-
escalator: each emits the reverse signal from inside _deliver(), and
each guards against consuming the type at the top of handle(). If you
add a third delivery subscriber, it MUST follow the same pattern.
Spec: docs/superpowers/specs/2026-04-30-notification-delivered-design.md.
"""

import logging
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)

# OTel tracing — defensive import so subscribers still work if obs/ or
# opentelemetry isn't installed (e.g. tests, CI).
try:
    from obs import get_tracer  # noqa: E402
    _TRACER = get_tracer("hermes.subscribers")
except Exception:  # pragma: no cover
    _TRACER = None


class _NoopSpan:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def set_attribute(self, *a, **kw): pass
    def record_exception(self, *a, **kw): pass
    def set_status(self, *a, **kw): pass
    def add_event(self, *a, **kw): pass


def _start_span(name: str):
    if _TRACER is None:
        return _NoopSpan()
    try:
        return _TRACER.start_as_current_span(name)
    except Exception:
        return _NoopSpan()


# Number of consecutive mark_handled failures for the same event before we
# dead-letter and force-advance the cursor past it. Set to 3 because:
#   - 1 or 2 failures could be transient WAL contention (auto-recovers)
#   - 3 in a row indicates persistent contention or a corrupt handled_events
#     table; better to lose at-least-once for one event than to flood the
#     downstream with the same event forever (2026-04-28 incident pattern).
# Note: this threshold is in *poll iterations*, not wall-clock time. With
# Hermes subscribers' typical poll_interval_seconds of 1-30, three iterations
# covers 3-90s — past SQLite's busy_timeout of 5000ms. Subscribers with very
# long poll intervals (>30s) should consider overriding to a higher value.
MARK_HANDLED_DEAD_LETTER_THRESHOLD: int = 3

# Cap for the in-process dedup set per subscriber. ~165 bytes per UUID4 entry
# would otherwise grow unbounded across the gateway's process lifetime
# (~1 GB / 30 days across 7 subscribers). 50k is far past any realistic
# single-poll-cycle dedup window — even a 50k-event flood would only
# evict entries from the *previous* flood, not the current batch.
HANDLED_THIS_PROCESS_MAXSIZE: int = 50_000


class _LRUEventIDSet:
    """Bounded LRU set of event_ids for in-process dedup.

    Wraps an OrderedDict so membership checks promote the entry to
    most-recent, and overflowing insertions evict the least-recently-used.
    Exposes the minimum surface area needed by BaseSubscriber.poll():
    ``in``, ``add``, ``clear``, and ``len()``.
    """

    def __init__(self, maxsize: int = HANDLED_THIS_PROCESS_MAXSIZE):
        self._od: "OrderedDict[str, None]" = OrderedDict()
        self._maxsize = maxsize

    def __contains__(self, event_id: str) -> bool:
        if event_id in self._od:
            self._od.move_to_end(event_id)
            return True
        return False

    def add(self, event_id: str) -> None:
        if event_id in self._od:
            self._od.move_to_end(event_id)
            return
        self._od[event_id] = None
        if len(self._od) > self._maxsize:
            self._od.popitem(last=False)

    def clear(self) -> None:
        self._od.clear()

    def __len__(self) -> int:
        return len(self._od)


class BaseSubscriber(ABC):
    """Abstract base class for event bus subscribers.

    Subclasses must define:
      - subscriber_id: unique string identifier
      - poll_interval_seconds: how often to poll (used by the runner)
      - handle(event): process a single event

    Optionally override:
      - event_types: list of EventType to filter (None = all)
      - min_priority: minimum Priority to receive (None = all)
    """

    subscriber_id: str = ""
    poll_interval_seconds: int = 5
    event_types: Optional[List[EventType]] = None
    min_priority: Optional[Priority] = None

    # Circuit breaker: overridable on subclasses for tighter/looser behaviour
    CIRCUIT_BREAKER_ERROR_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 300

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._consecutive_errors: int = 0
        self._quarantined_until: Optional[float] = None  # monotonic timestamp
        # 2026-04-28 incident: in-process dedup set. Prevents handle() re-firing
        # for an event already processed in this process even if the bus-level
        # `handled_events` row failed to write (WAL lock, disk full, etc.).
        # Cleared on process restart — the SQLite-level `is_handled` check
        # provides cross-restart durability when it can.
        # LRU-bounded so memory stays flat over long gateway uptimes.
        self._handled_this_process: _LRUEventIDSet = _LRUEventIDSet()
        # Tracks consecutive mark_handled failures per event so we can dead-letter
        # and advance past an event whose handle() succeeds but whose mark fails.
        self._mark_handled_failures: Dict[str, int] = {}

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Process a single event.  Exceptions are caught and logged."""

    def poll(self) -> int:
        """Fetch and process new events since last cursor.  Returns count processed.

        Respects the circuit breaker: if the subscriber is currently
        quarantined (after too many consecutive errors), returns 0 without
        invoking handle().  The quarantine clears automatically when the
        cooldown expires.
        """
        # Circuit breaker gate
        if self._quarantined_until is not None:
            if time.monotonic() < self._quarantined_until:
                return 0  # still in cooldown
            # Cooldown elapsed — leave quarantine, reset counter, try again
            logger.info(
                "Subscriber %s: circuit breaker cooldown elapsed, resuming",
                self.subscriber_id,
            )
            self._quarantined_until = None
            self._consecutive_errors = 0

        events = self.bus.subscribe(
            subscriber_id=self.subscriber_id,
            event_types=self.event_types,
            min_priority=self.min_priority,
        )
        if not events:
            return 0

        processed_ids = []
        for event in events:
            # 2026-04-28 dedup: check in-process set first (fast, can't fail), then
            # bus-level handled_events table (cross-restart durable but can fail
            # under WAL lock). Either short-circuits the handle() call.
            if event.event_id in self._handled_this_process:
                processed_ids.append(event.event_id)
                continue
            # Fail-open: if is_handled() raises (WAL lock, corrupt handled_events),
            # proceed to handle the event. Better to re-fire an idempotent handler
            # than to strand the cursor — and Task 7's dead-letter escalation
            # breaks the loop if mark_handled keeps failing.
            try:
                already_handled = self.bus.is_handled(self.subscriber_id, event.event_id)
            except Exception:
                logger.exception("Failed to query is_handled for %s", event.event_id)
                already_handled = False
            if already_handled:
                self._handled_this_process.add(event.event_id)
                processed_ids.append(event.event_id)
                continue
            # OTel: wrap handle() in a span. One span per subscriber-event
            # invocation. Breaker + dead-letter logic lives inside the span's
            # except block so a failure is recorded on the span AND drives the
            # breaker. Span attributes + exception are safe for any tracer.
            with _start_span(f"subscriber.handle:{self.subscriber_id}") as _span:
                _span.set_attribute("subscriber.id", self.subscriber_id)
                _span.set_attribute("event.id", event.event_id)
                try:
                    _span.set_attribute(
                        "event.type",
                        event.event_type.type_string
                        if hasattr(event.event_type, "type_string")
                        else str(event.event_type),
                    )
                    _span.set_attribute("event.source", str(getattr(event, "source", "")))
                    _span.set_attribute("event.priority", str(getattr(event, "priority", "")))
                except Exception:
                    pass
                try:
                    self.handle(event)
                    self._consecutive_errors = 0  # any success resets the counter
                    # In-process dedup recorded BEFORE the bus call so even if
                    # mark_handled fails (WAL lock, disk full), this process
                    # cannot re-fire handle() for the same event.
                    self._handled_this_process.add(event.event_id)
                    # SR-101: mark handled AFTER success. Wrapped so a bus-level
                    # failure doesn't cascade into the subscriber.
                    try:
                        self.bus.mark_handled(self.subscriber_id, event.event_id)
                        # Success: clear any prior failure counter.
                        self._mark_handled_failures.pop(event.event_id, None)
                    except Exception as _mh_exc:
                        logger.exception("Failed to mark handled for %s", event.event_id)
                        # See Task 7 for the dead-letter escalation logic.
                        self._on_mark_handled_failure(event.event_id, _mh_exc)
                except Exception as exc:
                    _span.record_exception(exc)
                    try:
                        from opentelemetry.trace import Status, StatusCode
                        _span.set_status(Status(StatusCode.ERROR, str(exc)))
                    except Exception:
                        pass
                    logger.exception(
                        "Subscriber %s failed to handle event %s (%s)",
                        self.subscriber_id, event.event_id, event.event_type.type_string,
                    )
                    self._consecutive_errors += 1
                    # SR-109: record per-(event, subscriber) dead-letter for
                    # observability. Never cascade a bus error into the subscriber.
                    try:
                        self.bus.record_dead_letter(
                            self.subscriber_id,
                            event.event_id,
                            f"{type(exc).__name__}: {exc}",
                        )
                    except Exception:
                        logger.exception("Failed to record dead-letter for %s", event.event_id)
                    if self._consecutive_errors >= self.CIRCUIT_BREAKER_ERROR_THRESHOLD:
                        # Trip the breaker.  Ack what we successfully processed
                        # so those events don't re-appear in lag, then bail out.
                        self._trip_circuit_breaker()
                        processed_ids.append(event.event_id)
                        self.bus.ack(self.subscriber_id, processed_ids)
                        return len(processed_ids)
            processed_ids.append(event.event_id)

        self.bus.ack(self.subscriber_id, processed_ids)
        return len(events)

    def _on_mark_handled_failure(self, event_id: str, exc: Exception) -> None:
        """Track repeated mark_handled failures and dead-letter past the threshold.

        At MARK_HANDLED_DEAD_LETTER_THRESHOLD consecutive failures for the same
        event, write a dead-letter row with reason ``mark_handled_lock_failure``.
        The caller (poll()) is responsible for adding event_id to processed_ids
        so the cursor advances past it on the next ack(). The at-least-once
        delivery contract is technically violated for this one event — but the
        alternative is the 2026-04-28-style infinite re-handle loop.
        """
        count = self._mark_handled_failures.get(event_id, 0) + 1
        self._mark_handled_failures[event_id] = count

        if count >= MARK_HANDLED_DEAD_LETTER_THRESHOLD:
            try:
                self.bus.record_dead_letter(
                    self.subscriber_id,
                    event_id,
                    f"mark_handled_lock_failure (after {count} attempts): "
                    f"{type(exc).__name__}: {exc}",
                )
                logger.error(
                    "Subscriber %s: dead-lettered event %s after %d consecutive "
                    "mark_handled failures (last error: %s)",
                    self.subscriber_id, event_id, count, exc,
                )
            except Exception:
                logger.exception(
                    "Subscriber %s: failed to record mark_handled dead-letter for %s",
                    self.subscriber_id, event_id,
                )
            # Reset the counter so a future re-emit of the same event_id (very
            # unlikely — UUIDs collide ~never) gets a fresh window.
            self._mark_handled_failures.pop(event_id, None)

    def _is_quarantined(self) -> bool:
        """Return True iff the circuit breaker is currently open."""
        if self._quarantined_until is None:
            return False
        return time.monotonic() < self._quarantined_until

    def _trip_circuit_breaker(self) -> None:
        """Enter quarantine and emit a single agent_error event.

        Called when consecutive_errors reaches the threshold.  Emits the
        error inside a try/except so a bus failure doesn't cascade.
        """
        self._quarantined_until = time.monotonic() + self.CIRCUIT_BREAKER_COOLDOWN_SECONDS
        logger.error(
            "Subscriber %s quarantined for %ds after %d consecutive errors",
            self.subscriber_id,
            self.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            self._consecutive_errors,
        )
        try:
            self.bus.emit(
                event_type=EventType.AGENT_ERROR,
                source="event-bus",
                payload={
                    "subscriber_id": self.subscriber_id,
                    "consecutive_errors": self._consecutive_errors,
                    "cooldown_seconds": self.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
                    "error": (
                        f"Subscriber '{self.subscriber_id}' quarantined after "
                        f"{self._consecutive_errors} consecutive errors; "
                        f"cooling down {self.CIRCUIT_BREAKER_COOLDOWN_SECONDS}s"
                    ),
                },
                priority=Priority.HIGH,
            )
        except Exception:
            logger.exception("Failed to emit circuit-breaker agent_error")

    def lag_report(self) -> int:
        """Return how many bus events this subscriber is behind head.

        Default implementation queries the bus using the subscriber's own
        ``event_types`` / ``min_priority`` filters. Filesystem-driven
        subscribers (which never consume bus events) should override this
        to return 0 — otherwise their cursor sits at 0 forever and the
        registry-level lag check reports the full event count as backlog,
        triggering false HIGH-priority lag alerts every check interval.
        """
        return self.bus.subscriber_lag(
            self.subscriber_id,
            event_types=self.event_types,
            min_priority=self.min_priority,
        )

    def startup(self) -> None:
        """Called once when the subscriber is registered.  Override for init logic."""

    def shutdown(self) -> None:
        """Called once on gateway shutdown.  Override for cleanup logic."""


class SubscriberRegistry:
    """Manages a set of subscribers and coordinates polling."""

    def __init__(self):
        self.subscribers: List[BaseSubscriber] = []

    def register(self, subscriber: BaseSubscriber) -> None:
        """Add a subscriber to the registry."""
        self.subscribers.append(subscriber)
        logger.info("Registered subscriber: %s", subscriber.subscriber_id)

    def poll_all(self) -> Dict[str, int]:
        """Poll all subscribers and return {subscriber_id: events_processed}."""
        results = {}
        for sub in self.subscribers:
            try:
                count = sub.poll()
                results[sub.subscriber_id] = count
            except Exception:
                logger.exception("Failed to poll subscriber %s", sub.subscriber_id)
                results[sub.subscriber_id] = 0
        return results

    def startup_all(self) -> None:
        """Call startup() on all subscribers."""
        for sub in self.subscribers:
            try:
                sub.startup()
            except Exception:
                logger.exception("Subscriber %s startup failed", sub.subscriber_id)

    def shutdown_all(self) -> None:
        """Call shutdown() on all subscribers."""
        for sub in self.subscribers:
            try:
                sub.shutdown()
            except Exception:
                logger.exception("Subscriber %s shutdown failed", sub.subscriber_id)

    def lag_report(self) -> Dict[str, int]:
        """Return {subscriber_id: events_behind_head} for all registered subscribers.

        A growing value for any subscriber indicates it's falling behind
        (slow handlers, crashed process, or a bug in poll()).  Operators
        should expect values near zero in a healthy system.
        """
        report: Dict[str, int] = {}
        for sub in self.subscribers:
            try:
                report[sub.subscriber_id] = sub.lag_report()
            except Exception:
                logger.exception("Lag query failed for %s", sub.subscriber_id)
                report[sub.subscriber_id] = -1  # sentinel for unknown
        return report
