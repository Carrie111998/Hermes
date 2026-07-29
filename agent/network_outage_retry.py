"""Persistent retry policy for provider transport outages and stalls.

The ordinary API retry loop is intentionally bounded.  This module adds an
opt-in second tier for genuine status-less transport failures and explicit
provider-response watchdog timeouts: after the quick retries and client
rebuild/fallback paths are exhausted, keep the active turn alive and retry at
a slow fixed cadence.

The predicate is deliberately narrow.  HTTP responses, provider overload/rate
limits, auth/billing errors, deterministic TLS certificate failures, context
errors must never enter an unbounded outage loop.  Explicit no-byte/no-event
provider watchdog timeouts are included because they are indistinguishable to
the caller from a transient provider-path stall and the operator has opted in
to keeping the task alive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from agent.error_classifier import ClassifiedError, FailoverReason


_PROVIDER_STALL_WATCHDOG_PATTERNS = (
    "codex stream produced no bytes within",
    "codex stream produced no sse events for",
    "non-streaming api call timed out after",
)


@dataclass(frozen=True)
class NetworkOutageRetryPolicy:
    """Configuration for slow provider-network outage retries.

    ``max_wait_seconds == 0`` means no wall-clock limit.  Interrupts still
    abort immediately, so an infinite outage policy never traps an operator.
    """

    enabled: bool = False
    interval_seconds: float = 300.0
    max_wait_seconds: float = 0.0

    @classmethod
    def from_config(cls, raw: Any) -> "NetworkOutageRetryPolicy":
        if not isinstance(raw, Mapping):
            raw = {}

        enabled_raw = raw.get("enabled", False)
        if isinstance(enabled_raw, str):
            enabled = enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(enabled_raw)

        # ``sleep_seconds`` was used by an earlier local config shape.  Keep it
        # as a compatibility alias so an enabled policy cannot silently fall
        # back to a different cadence after an update.
        interval_raw = raw.get("interval_seconds", raw.get("sleep_seconds", 300.0))
        try:
            interval = float(interval_raw)
        except (TypeError, ValueError):
            interval = 300.0
        # Avoid a malformed config creating a hot retry loop.
        interval = max(interval, 1.0)

        try:
            max_wait = float(raw.get("max_wait_seconds", 0.0))
        except (TypeError, ValueError):
            max_wait = 0.0
        max_wait = max(max_wait, 0.0)

        return cls(
            enabled=enabled,
            interval_seconds=interval,
            max_wait_seconds=max_wait,
        )


def is_provider_network_outage(
    error: Exception,
    classified: ClassifiedError,
) -> bool:
    """Return True for an opted-in status-less transport/stall failure."""

    # Any HTTP response means the network path reached a server. Server 5xx,
    # overload, rate limit, auth, and billing keep their existing bounded paths.
    if classified.status_code is not None:
        return False
    if getattr(error, "status_code", None) is not None:
        return False
    response = getattr(error, "response", None)
    if getattr(response, "status_code", None) is not None:
        return False

    # Explicit provider response watchdogs are part of the opted-in policy.
    # Keep them active even if a future classifier refinement stops labelling
    # one of these exact no-byte/no-event failures as a generic timeout.
    message = str(error).lower()
    if any(pattern in message for pattern in _PROVIDER_STALL_WATCHDOG_PATTERNS):
        return True

    return classified.reason == FailoverReason.timeout and classified.retryable


def outage_retry_wait_seconds(
    policy: NetworkOutageRetryPolicy,
    *,
    elapsed_seconds: float,
) -> Optional[float]:
    """Return the next wait duration, or None when outage retry is exhausted."""

    if not policy.enabled:
        return None
    if policy.max_wait_seconds <= 0:
        return policy.interval_seconds

    remaining = policy.max_wait_seconds - max(elapsed_seconds, 0.0)
    if remaining <= 0:
        return None
    return min(policy.interval_seconds, remaining)


def wait_for_outage_retry(agent: Any, seconds: float, *, cycle: int) -> bool:
    """Sleep interruptibly while keeping gateway activity heartbeats alive.

    Returns False when the operator interrupts the turn, otherwise True.
    """

    deadline = time.monotonic() + max(float(seconds), 0.0)
    next_touch = time.monotonic() + 30.0
    while True:
        now = time.monotonic()
        if now >= deadline:
            return True
        if getattr(agent, "_interrupt_requested", False):
            return False
        if now >= next_touch:
            try:
                agent._touch_activity(
                    f"provider network outage retry wait (cycle {cycle}), "
                    f"{max(0, int(deadline - now))}s remaining"
                )
            except Exception:
                pass
            next_touch = now + 30.0
        time.sleep(min(0.2, max(0.0, deadline - now)))
