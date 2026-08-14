"""Discord streaming-delivery state machine (feature M7).

Pure state machine -- no network I/O. Guards the lifecycle of streaming a
Discord message: typing indicator -> streaming chunks -> done/failed.

States:
    IDLE      -- no delivery session in progress
    TYPING    -- delivery began, typing indicator active
    STREAMING -- chunks may be appended
    DONE      -- delivery finished (terminal, not re-entered)
    FAILED    -- delivery aborted (terminal, not re-entered)

Invariants:
    * ``finish()`` may only be called once per session (duplicate final
      response guard): a second call raises :class:`DeliveryStateError`.
    * Empty chunks are no-ops and never drop previously appended content.
    * Finishing with empty content is allowed only when no non-empty chunk
      was appended; otherwise the stream transitions to DONE with its
      content intact (never silently dropped).
"""

from __future__ import annotations


class DeliveryStateError(ValueError):
    """Raised when a delivery-state transition is invalid."""


class DeliveryState:
    """State machine for a single Discord streaming-delivery session."""

    IDLE = "IDLE"
    TYPING = "TYPING"
    STREAMING = "STREAMING"
    DONE = "DONE"
    FAILED = "FAILED"

    # States in which a delivery session is considered active.
    _ACTIVE = frozenset((TYPING, STREAMING))

    def __init__(self) -> None:
        self._state = self.IDLE
        self._chunks: list[str] = []
        self._error: object | None = None

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def begin(self) -> str:
        """IDLE -> TYPING. Start a delivery session."""
        if self._state is not self.IDLE:
            raise DeliveryStateError(
                f"begin() requires IDLE state, got {self._state!r}"
            )
        self._state = self.TYPING
        return self._state

    def start_stream(self) -> str:
        """TYPING -> STREAMING. Chunks may now be appended."""
        if self._state is not self.TYPING:
            raise DeliveryStateError(
                f"start_stream() requires TYPING state, got {self._state!r}"
            )
        self._state = self.STREAMING
        return self._state

    def append(self, chunk: str) -> str:
        """STREAMING -> STREAMING. Append a chunk; empty chunks are no-ops."""
        if self._state is not self.STREAMING:
            raise DeliveryStateError(
                f"append() requires STREAMING state, got {self._state!r}"
            )
        if chunk is None:
            raise DeliveryStateError("append() requires a str chunk, got None")
        chunk = str(chunk)
        if chunk:
            self._chunks.append(chunk)
        return self._state

    def finish(self) -> str:
        """STREAMING -> DONE. May be called only once per session."""
        if self._state is not self.STREAMING:
            raise DeliveryStateError(
                f"finish() requires STREAMING state, got {self._state!r} "
                "(duplicate final-response guard)"
            )
        self._state = self.DONE
        return self._state

    def fail(self, err: object | None = None) -> str:
        """Any active state -> FAILED. Aborts the delivery session."""
        if self._state not in self._ACTIVE:
            raise DeliveryStateError(
                f"fail() requires an active state (TYPING/STREAMING), "
                f"got {self._state!r}"
            )
        self._state = self.FAILED
        self._error = err
        return self._state

    def reset(self) -> str:
        """Any state -> IDLE. Clears all session state."""
        self._state = self.IDLE
        self._chunks = []
        self._error = None
        return self._state

    # ------------------------------------------------------------------
    # Observers
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def delivered_chunks(self) -> int:
        """Number of non-empty chunks appended in this session."""
        return len(self._chunks)

    @property
    def has_content(self) -> bool:
        """True if any non-empty chunk was appended."""
        return bool(self._chunks)

    @property
    def error(self) -> object | None:
        """Error recorded by the most recent ``fail()`` call, if any."""
        return self._error
