"""Bounded SQLite busy-timeout configuration for ARCH-001.

Application-level contention retries belong to ARCH-004.  This module only
validates and applies SQLite's connection-level busy timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

MIN_BUSY_TIMEOUT_MS = 1
MAX_BUSY_TIMEOUT_MS = 10_000


@dataclass(frozen=True)
class RetryConfig:
    busy_timeout_ms: int = 5_000
    max_retries: int = 5
    jitter_min_ms: int = 10
    jitter_max_ms: int = 250

    def __post_init__(self) -> None:
        if not MIN_BUSY_TIMEOUT_MS <= self.busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
            raise ValueError(
                f"busy_timeout_ms must be in "
                f"[{MIN_BUSY_TIMEOUT_MS}, {MAX_BUSY_TIMEOUT_MS}]"
            )
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.jitter_min_ms < 0 or self.jitter_max_ms < self.jitter_min_ms:
            raise ValueError("jitter bounds must satisfy 0 <= min <= max")


DEFAULT_RETRY_CONFIG = RetryConfig()


def apply_busy_timeout(
    conn: sqlite3.Connection, config: RetryConfig = DEFAULT_RETRY_CONFIG
) -> None:
    """Apply the validated connection-level busy timeout exactly once."""

    conn.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")


def no_retry_hook(write_fn, *args, **kwargs):
    """Invoke a write once; ARCH-004 owns any application retry loop."""

    return write_fn(*args, **kwargs)
