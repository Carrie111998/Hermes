"""Minimal circuit breaker for the JobOps client.

Trips open after N consecutive failures. After `reset_timeout_seconds` of being
open, the next call enters a half-open state — if it succeeds, the breaker
fully closes; if it fails, the breaker stays open for another timeout window.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class CircuitBreakerOpen(Exception):
    """Raised by guard() when the breaker is open and rejecting calls."""


class SimpleCircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5, reset_timeout_seconds: float = 300.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout_seconds = reset_timeout_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_timeout_seconds:
            # Half-open: allow next call through
            self._opened_at = None
            self._consecutive_failures = 0
            logger.info("circuit-breaker: reset timeout elapsed, entering half-open")
            return False
        return True

    def guard(self) -> None:
        """Raise CircuitBreakerOpen if calls are currently rejected."""
        if self.is_open():
            raise CircuitBreakerOpen(
                f"circuit breaker open ({self._consecutive_failures} consecutive failures, "
                f"will half-open in {self.reset_timeout_seconds}s)"
            )

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.warning(
                "circuit-breaker: TRIPPED after %d consecutive failures",
                self._consecutive_failures,
            )

    def record_success(self) -> None:
        if self._consecutive_failures > 0 or self._opened_at is not None:
            logger.info("circuit-breaker: success — resetting failure counter")
        self._consecutive_failures = 0
        self._opened_at = None
