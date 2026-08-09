"""A bounded, unregistered Bridge that contains delivery failure locally."""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from plugins.agentops.control.events import EventValidationError, canonical_json
from plugins.agentops.control.models import EventEnvelope


@dataclass(frozen=True)
class BridgeResult:
    delivered: bool
    queued: bool
    dropped: int


class BoundedBridgeBuffer:
    """No hooks are registered: callers opt in and retain their own flow."""

    def __init__(self, capacity: int = 256) -> None:
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("invalid bridge capacity")
        self.capacity = capacity
        self._pending: deque[str] = deque()
        self._lock = threading.Lock()
        self._dropped = 0

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    @staticmethod
    def _canonical_event(event: EventEnvelope) -> str:
        """Validate then detach nested payload data into immutable canonical bytes."""
        if not isinstance(event, EventEnvelope):
            raise EventValidationError("event validation failed")
        serialized = canonical_json(event.to_dict())
        EventEnvelope.from_dict(json.loads(serialized))
        return serialized

    @staticmethod
    def _delivery_copy(serialized: str) -> EventEnvelope:
        """Revalidate every outbound copy; a consumer cannot mutate queued data."""
        return EventEnvelope.from_dict(json.loads(serialized))

    def _enqueue(self, serialized: str) -> bool:
        self._delivery_copy(serialized)
        with self._lock:
            if len(self._pending) >= self.capacity:
                self._pending.popleft()
                self._dropped += 1
            self._pending.append(serialized)
        return True

    def publish(self, event: EventEnvelope, deliver: Callable[[EventEnvelope], None]) -> BridgeResult:
        """Return status on consumer failure; never propagate it to the caller."""
        try:
            serialized = self._canonical_event(event)
        except (EventValidationError, TypeError, ValueError):
            return BridgeResult(delivered=False, queued=False, dropped=self.dropped)
        try:
            deliver(self._delivery_copy(serialized))
            return BridgeResult(delivered=True, queued=False, dropped=self.dropped)
        except Exception:
            try:
                queued = self._enqueue(serialized)
            except (EventValidationError, TypeError, ValueError):
                queued = False
            return BridgeResult(delivered=False, queued=queued, dropped=self.dropped)

    def drain(self, deliver: Callable[[EventEnvelope], None]) -> int:
        delivered = 0
        while True:
            with self._lock:
                if not self._pending:
                    return delivered
                serialized = self._pending[0]
            try:
                event = self._delivery_copy(serialized)
                deliver(event)
            except Exception:
                return delivered
            with self._lock:
                if self._pending and self._pending[0] == serialized:
                    self._pending.popleft()
                    delivered += 1
