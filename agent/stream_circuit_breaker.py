"""
stream_circuit_breaker — Consecutive-failure circuit breaker for streaming API calls.

Tracks consecutive failures per (provider, model) pair.  After N failures within
a configurable time window, marks the provider dead via DeadProviderRegistry so
the existing fallback chain (try_activate_fallback) skips it.

Designed for the deepseek-v4-flash/opencode-go streaming failure pattern where
a brief upstream outage triggers many sequential failures, burning retry budget
and API cost.  The breaker trips before the retry budget is exhausted, shifting
traffic to a fallback provider immediately.

Usage:
    from agent.stream_circuit_breaker import (
        record_stream_failure,
        record_stream_success,
        reset_circuit_breaker,
    )

    # In conversation_loop retry handler:
    if record_stream_failure(agent, provider, model, "Broken pipe"):
        logger.warning("Circuit breaker tripped for %s/%s", provider, model)
        # fallback will be triggered on next retry attempt

    # On successful API response:
    record_stream_success(agent, provider, model)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
# Number of consecutive failures within the window before tripping the breaker.
TRIP_THRESHOLD = 3

# Time window (seconds) for counting consecutive failures.
TRIP_WINDOW_SECONDS = 300  # 5 minutes

# ── In-memory state ─────────────────────────────────────────────────────────
# Maps (provider_lower, model) → (failure_count, first_failure_time_monotonic)
# Thread-safe via _lock.
_state: Dict[Tuple[str, str], Tuple[int, float]] = {}
_lock = threading.Lock()


def _key(provider: str, model: str) -> Tuple[str, str]:
    return (provider.lower().strip(), model.strip())


def record_stream_failure(
    agent,
    provider: str,
    model: str,
    error_hint: str = "",
) -> bool:
    """Record a streaming failure and return True if the circuit breaker tripped.

    Trips (marks provider dead) when TRIP_THRESHOLD consecutive failures occur
    within TRIP_WINDOW_SECONDS.  On trip, calls ``dead_registry.mark_provider_dead()``
    which causes ``try_activate_fallback`` to skip this provider.

    Resets the counter for any (provider, model) when the window expires without
    reaching the threshold — a burst of 3 failures, then silence for 5 minutes,
    resets so an isolated spike doesn't permanently degrade the provider.
    """
    k = _key(provider, model)
    now = time.monotonic()
    with _lock:
        count, window_start = _state.get(k, (0, now))
        if now - window_start > TRIP_WINDOW_SECONDS:
            # Window expired — reset
            count = 0
            window_start = now
        count += 1
        _state[k] = (count, window_start)

    if count < TRIP_THRESHOLD:
        logger.debug(
            "Circuit breaker: %s/%s failure %d/%d (window=%.0fs) %s",
            provider, model, count, TRIP_THRESHOLD, TRIP_WINDOW_SECONDS, error_hint,
        )
        return False

    # ── Trip the breaker ────────────────────────────────────────────────
    logger.warning(
        "Circuit breaker TRIPPED for %s/%s after %d consecutive failures "
        "within %.0fs — marking provider dead. Last error: %s",
        provider, model, count, TRIP_WINDOW_SECONDS, error_hint,
    )

    _dead_reg = getattr(agent, "_dead_registry", None)
    if _dead_reg is not None:
        try:
            _dead_reg.mark_provider_dead(
                provider,
                model,
                reason=f"circuit_breaker: {count} consecutive streaming failures in {TRIP_WINDOW_SECONDS}s",
            )
        except Exception as exc:
            logger.error("Circuit breaker: failed to mark provider dead: %s", exc)

    # Reset counter after tripping so it can accumulate again after TTL expiry.
    with _lock:
        _state[k] = (0, now)

    return True


def record_stream_success(agent, provider: str, model: str) -> None:
    """Record a successful streaming response — resets the consecutive-failure counter.

    Call this when the API call completes successfully (before response validation,
    so malformed responses don't count as "success").
    """
    k = _key(provider, model)
    with _lock:
        if k in _state:
            count, _ = _state[k]
            if count > 0:
                logger.debug(
                    "Circuit breaker: %s/%s success — resetting counter (was %d)",
                    provider, model, count,
                )
            del _state[k]


def reset_circuit_breaker(provider: str, model: str) -> None:
    """Manually reset the circuit breaker for a specific provider/model."""
    k = _key(provider, model)
    with _lock:
        _state.pop(k, None)


def get_circuit_state(provider: str, model: str) -> Optional[Dict]:
    """Return the current circuit state for a provider/model, or None.

    Returns dict with ``failure_count`` and ``window_remaining`` (seconds) if
    the breaker is tracking failures, or None if idle.
    """
    k = _key(provider, model)
    with _lock:
        entry = _state.get(k)
        if entry is None:
            return None
        count, window_start = entry
        now = time.monotonic()
        elapsed = now - window_start
        remaining = max(0.0, TRIP_WINDOW_SECONDS - elapsed)
        return {
            "failure_count": count,
            "window_remaining_s": remaining,
            "tripped": count >= TRIP_THRESHOLD,
        }
