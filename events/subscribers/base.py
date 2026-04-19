"""Base subscriber class and registry for the Hermes Event Bus.

Subscribers independently consume events from the bus via poll().
Each subscriber tracks its own cursor so multiple subscribers
can process the same events without interference.

Circuit breaker: if a subscriber's handle() raises CIRCUIT_BREAKER_ERROR_THRESHOLD
times in a row, the subscriber enters quarantine for CIRCUIT_BREAKER_COOLDOWN_SECONDS.
During quarantine, poll() is a no-op — events pile up in the backlog (visible via
subscriber_lag) instead of repeatedly invoking a broken handler.  A single
agent_error event is emitted on the transition into quarantine.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from events.bus import EventBus
from events.schema import Event, EventType, Priority

logger = logging.getLogger(__name__)


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
            # SR-101: at-least-once redelivery dedup. If a prior poll already
            # processed this event successfully, skip handle() but still ack
            # the cursor so the event doesn't re-appear forever.
            if self.bus.is_handled(self.subscriber_id, event.event_id):
                processed_ids.append(event.event_id)
                continue
            try:
                self.handle(event)
                self._consecutive_errors = 0  # any success resets the counter
                # SR-101: mark handled AFTER success. Wrapped so a bus-level
                # failure doesn't cascade into the subscriber.
                try:
                    self.bus.mark_handled(self.subscriber_id, event.event_id)
                except Exception:
                    logger.exception("Failed to mark handled for %s", event.event_id)
            except Exception as exc:
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
                report[sub.subscriber_id] = sub.bus.subscriber_lag(
                    sub.subscriber_id,
                    event_types=sub.event_types,
                    min_priority=sub.min_priority,
                )
            except Exception:
                logger.exception("Lag query failed for %s", sub.subscriber_id)
                report[sub.subscriber_id] = -1  # sentinel for unknown
        return report
