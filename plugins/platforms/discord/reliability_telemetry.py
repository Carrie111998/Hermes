"""Discord local reliability telemetry (local-only, zero outbound).

Pure in-memory counters guarded by a lock. No network and no disk I/O
unless an explicit sink is provided by the caller.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, Optional

# Predefined reliability event names.
CONNECT = "connect"
READY = "ready"
RECONNECT = "reconnect"
SYNC = "sync"
RATE_LIMIT_429 = "rate_limit_429"
DROPPED_ATTACHMENT = "dropped_attachment"
DELIVERY_FAILURE = "delivery_failure"
VOICE_ATTACH = "voice_attach"
VOICE_DETACH = "voice_detach"
UNAUTHORIZED = "unauthorized"

KNOWN_EVENTS = frozenset(
    {
        CONNECT,
        READY,
        RECONNECT,
        SYNC,
        RATE_LIMIT_429,
        DROPPED_ATTACHMENT,
        DELIVERY_FAILURE,
        VOICE_ATTACH,
        VOICE_DETACH,
        UNAUTHORIZED,
    }
)


class ReliabilityTelemetryError(ValueError):
    """Raised when an unknown or empty telemetry event name is recorded."""


class TelemetrySink:
    """No-op sink interface.

    Local-only default: ``emit`` does nothing, so telemetry never leaves
    the process unless a concrete sink is supplied.
    """

    def emit(self, snapshot: Dict[str, int]) -> None:
        """Handle a telemetry snapshot. Default implementation: no-op."""
        del snapshot  # no-op sink; snapshot is intentionally discarded


class CallbackSink(TelemetrySink):
    """Sink that forwards each snapshot to a callable (testability hook)."""

    def __init__(self, callback: Callable[[Dict[str, int]], None]) -> None:
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback

    def emit(self, snapshot: Dict[str, int]) -> None:
        self._callback(snapshot)


class ReliabilityTelemetry:
    """Thread-safe in-memory counters for Discord reliability events."""

    def __init__(self, sink: Optional[TelemetrySink] = None) -> None:
        self._counts: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._sink = sink if sink is not None else TelemetrySink()

    def record_event(self, name: str, count: int = 1) -> None:
        """Increment ``name`` by ``count`` (default 1).

        Unknown or empty names are rejected so misspelled telemetry fails
        loudly instead of silently drifting.
        """
        if not isinstance(name, str) or name not in KNOWN_EVENTS:
            raise ReliabilityTelemetryError(
                f"unknown or empty telemetry event name: {name!r}"
            )
        if count < 0:
            raise ReliabilityTelemetryError("count must be non-negative")
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + count

    def get_count(self, name: str) -> int:
        """Return the recorded count for ``name`` (0 if never recorded)."""
        with self._lock:
            return self._counts.get(name, 0)

    def snapshot(self) -> Dict[str, int]:
        """Return a copy of all recorded name -> count pairs."""
        with self._lock:
            return dict(self._counts)

    def flush(self) -> Dict[str, int]:
        """Emit the current snapshot to the sink and return it."""
        snap = self.snapshot()
        self._sink.emit(snap)
        return snap
