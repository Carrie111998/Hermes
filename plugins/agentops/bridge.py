"""A bounded, unregistered Bridge that contains delivery failure locally."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from plugins.agentops.control.events import EventValidationError
from plugins.agentops.control.models import EventEnvelope


@dataclass(frozen=True)
class BridgeResult:
    delivered: bool
    queued: bool
    dropped: int


class BoundedBridgeBuffer:
    """No hooks are registered: callers opt in and retain their own control flow."""

    def __init__(self, capacity: int = 256) -> None:
        if not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("invalid bridge capacity")
        self.capacity = capacity
        self._pending: deque[EventEnvelope] = deque()
        self.dropped = 0

    @property
    def depth(self) -> int:
        return len(self._pending)

    @staticmethod
    def _validated(event: EventEnvelope) -> EventEnvelope:
        if not isinstance(event, EventEnvelope):
            raise EventValidationError("event validation failed")
        return EventEnvelope.from_dict(event.to_dict())

    def _enqueue(self, event: EventEnvelope) -> bool:
        if len(self._pending) >= self.capacity:
            self._pending.popleft()
            self.dropped += 1
        self._pending.append(event)
        return True

    def publish(self, event: EventEnvelope, deliver: Callable[[EventEnvelope], None]) -> BridgeResult:
        """Return status on consumer failure; never propagate it to the caller."""
        try:
            validated = self._validated(event)
        except EventValidationError:
            return BridgeResult(delivered=False, queued=False, dropped=self.dropped)
        try:
            deliver(validated)
            return BridgeResult(delivered=True, queued=False, dropped=self.dropped)
        except Exception:
            self._enqueue(validated)
            return BridgeResult(delivered=False, queued=True, dropped=self.dropped)

    def drain(self, deliver: Callable[[EventEnvelope], None]) -> int:
        delivered = 0
        while self._pending:
            event = self._pending[0]
            try:
                deliver(event)
            except Exception:
                break
            self._pending.popleft()
            delivered += 1
        return delivered
