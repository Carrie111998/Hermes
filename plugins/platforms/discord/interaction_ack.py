"""Discord interaction ACK + error discipline.

Pure state logic for the Discord interaction lifecycle.  A Discord
interaction must be acknowledged (or deferred) inside Discord's 3-second
ACK window -- before any expensive work is started -- and can receive
exactly one initial response.
"""

from __future__ import annotations

import enum


class InteractionAckError(ValueError):
    """Raised when an interaction lifecycle transition is illegal."""


class InteractionState(enum.Enum):
    PENDING = "PENDING"
    ACKED = "ACKED"
    DEFERRED = "DEFERRED"
    RESPONDED = "RESPONDED"
    EXPIRED = "EXPIRED"


ACK_WINDOW_SECONDS = 3.0


class InteractionLifecycle:
    """Tracks the ACK/response lifecycle of a single Discord interaction.

    State machine::

        PENDING --ack--> ACKED --respond--> RESPONDED
        PENDING --defer-> DEFERRED --respond--> RESPONDED
        PENDING --(past 3s)--> EXPIRED (ack/defer raise InteractionAckError)

    Rules:
      * ``ack``/``defer`` only from PENDING, and only within the 3-second
        ACK window (``now - created_at <= 3.0``).
      * exactly one initial response: ``respond`` only from ACKED/DEFERRED,
        and a second ``respond`` raises.
    """

    def __init__(self, created_at: float) -> None:
        self.created_at = created_at
        self.state = InteractionState.PENDING

    def _enforce_ack_window(self, now: float) -> None:
        """Mark EXPIRED and raise when the 3s ACK window has lapsed."""
        if now - self.created_at > ACK_WINDOW_SECONDS:
            self.state = InteractionState.EXPIRED
            raise InteractionAckError(
                "interaction ACK window (3s) expired: "
                f"created_at={self.created_at!r}, now={now!r}"
            )

    def ack(self, now: float) -> None:
        """Acknowledge the interaction inside the 3s window (PENDING -> ACKED)."""
        if self.state is not InteractionState.PENDING:
            raise InteractionAckError(
                f"cannot ack interaction in state {self.state.value}"
            )
        self._enforce_ack_window(now)
        self.state = InteractionState.ACKED

    def defer(self, now: float) -> None:
        """Defer the interaction inside the 3s window (PENDING -> DEFERRED)."""
        if self.state is not InteractionState.PENDING:
            raise InteractionAckError(
                f"cannot defer interaction in state {self.state.value}"
            )
        self._enforce_ack_window(now)
        self.state = InteractionState.DEFERRED

    def respond(self, now: float) -> None:
        """Send the one initial response (ACKED/DEFERRED -> RESPONDED).

        Raises ``InteractionAckError`` if the interaction was already
        responded to (single-response invariant) or is not in a state
        from which an initial response is allowed.
        """
        if self.state is InteractionState.RESPONDED:
            raise InteractionAckError(
                "interaction already responded; only one initial response allowed"
            )
        if self.state not in (InteractionState.ACKED, InteractionState.DEFERRED):
            raise InteractionAckError(
                "cannot respond to interaction in state "
                f"{self.state.value}; must be acked or deferred first"
            )
        self.state = InteractionState.RESPONDED

    def is_within_ack_window(self, now: float) -> bool:
        """True while ``now`` is still inside the 3s ACK window (<= 3.0)."""
        return now - self.created_at <= ACK_WINDOW_SECONDS
