"""Bounded SQLite busy-timeout and contention retry policy for ARCH-004."""

from __future__ import annotations

from dataclasses import dataclass
import random
import sqlite3

MIN_BUSY_TIMEOUT_MS = 1
MAX_BUSY_TIMEOUT_MS = 10_000
DEFAULT_MAX_RETRIES = 2  # three total mutation attempts
MAX_APPLICATION_RETRIES = 2
DEFAULT_BASE_DELAY_MS = 100
DEFAULT_DELAY_CAP_MS = 500
DEFAULT_JITTER_MIN_MS = 0
DEFAULT_JITTER_MAX_MS = 50

_TRANSIENT_SQLITE_CODES = frozenset(
    code for code in (getattr(sqlite3, "SQLITE_BUSY", None), getattr(sqlite3, "SQLITE_LOCKED", None))
    if code is not None
)
_TRANSIENT_SQLITE_MESSAGES = frozenset({"database is locked", "database table is locked"})


@dataclass(frozen=True)
class RetryConfig:
    busy_timeout_ms: int = 5_000
    max_retries: int = DEFAULT_MAX_RETRIES
    base_delay_ms: int = DEFAULT_BASE_DELAY_MS
    delay_cap_ms: int = DEFAULT_DELAY_CAP_MS
    jitter_min_ms: int = DEFAULT_JITTER_MIN_MS
    jitter_max_ms: int = DEFAULT_JITTER_MAX_MS

    def __post_init__(self) -> None:
        if not MIN_BUSY_TIMEOUT_MS <= self.busy_timeout_ms <= MAX_BUSY_TIMEOUT_MS:
            raise ValueError(
                f"busy_timeout_ms must be in "
                f"[{MIN_BUSY_TIMEOUT_MS}, {MAX_BUSY_TIMEOUT_MS}]"
            )
        if not 0 <= self.max_retries <= MAX_APPLICATION_RETRIES:
            raise ValueError(
                f"max_retries must be in [0, {MAX_APPLICATION_RETRIES}]"
            )
        if self.base_delay_ms < 0:
            raise ValueError("base_delay_ms must be >= 0")
        if self.delay_cap_ms < self.base_delay_ms:
            raise ValueError("delay_cap_ms must be >= base_delay_ms")
        if self.jitter_min_ms < 0 or self.jitter_max_ms < self.jitter_min_ms:
            raise ValueError("jitter bounds must satisfy 0 <= min <= max")

    @property
    def max_attempts(self) -> int:
        """Return the total number of mutation attempts, including the first."""

        return self.max_retries + 1

    def delay_ms(self, retry_number: int) -> float:
        """Return bounded delay before retry number 1, 2, ... in milliseconds."""

        if retry_number < 1:
            raise ValueError("retry_number must be >= 1")
        exponential = min(
            self.delay_cap_ms,
            self.base_delay_ms * (2 ** (retry_number - 1)),
        )
        return exponential + random.uniform(self.jitter_min_ms, self.jitter_max_ms)


DEFAULT_RETRY_CONFIG = RetryConfig()


def apply_busy_timeout(
    conn: sqlite3.Connection, config: RetryConfig = DEFAULT_RETRY_CONFIG
) -> None:
    """Apply the validated connection-level busy timeout exactly once."""

    conn.execute(f"PRAGMA busy_timeout = {config.busy_timeout_ms}")


def no_retry_hook(write_fn, *args, **kwargs):
    """Invoke a write once for callers outside the runtime-state boundary."""

    return write_fn(*args, **kwargs)


def is_transient_sqlite_error(exc: BaseException) -> bool:
    """Classify only the closed SQLite contention failures as retryable.

    Python versions exposing ``sqlite_errorcode`` are authoritative.  The
    exact normalized message fallback is intentionally narrow and never
    returns the source message to callers.
    """

    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return code in _TRANSIENT_SQLITE_CODES
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    return str(exc).strip().casefold() in _TRANSIENT_SQLITE_MESSAGES
