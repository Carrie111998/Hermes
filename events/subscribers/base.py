"""Base subscriber class and registry for the Hermes Event Bus.

Subscribers independently consume events from the bus via poll().
Each subscriber tracks its own cursor so multiple subscribers
can process the same events without interference.
"""

import logging
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

    def __init__(self, bus: EventBus):
        self.bus = bus

    @abstractmethod
    def handle(self, event: Event) -> None:
        """Process a single event.  Exceptions are caught and logged."""

    def poll(self) -> int:
        """Fetch and process new events since last cursor.  Returns count processed."""
        events = self.bus.subscribe(
            subscriber_id=self.subscriber_id,
            event_types=self.event_types,
            min_priority=self.min_priority,
        )
        if not events:
            return 0

        processed_ids = []
        for event in events:
            try:
                self.handle(event)
            except Exception:
                logger.exception(
                    "Subscriber %s failed to handle event %s (%s)",
                    self.subscriber_id, event.event_id, event.event_type.type_string,
                )
            processed_ids.append(event.event_id)

        self.bus.ack(self.subscriber_id, processed_ids)
        return len(events)

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
                report[sub.subscriber_id] = sub.bus.subscriber_lag(sub.subscriber_id)
            except Exception:
                logger.exception("Lag query failed for %s", sub.subscriber_id)
                report[sub.subscriber_id] = -1  # sentinel for unknown
        return report
