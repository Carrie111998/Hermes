"""Discord route-aware rate-limit contract (pure logic, no network I/O).

This module models the rate-limit state machine and error taxonomy used by
the Discord API transport layer:

* :class:`RateLimitInfo` -- normalized view of a Discord 429 response.
* :func:`parse_rate_limit_response` -- strict parsing of a 429 body.
* :func:`is_retriable` -- route/method-aware retry classification.
* :class:`RouteBucket` -- per-route 429 cooldown tracker.
* :class:`TransportError` -- transport-level error (subclass of ValueError).

It is intentionally free of any network calls so it can be unit-tested in
isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

__all__ = [
    "RateLimitInfo",
    "RouteBucket",
    "TransportError",
    "is_retriable",
    "parse_rate_limit_response",
]


class TransportError(ValueError):
    """Raised when a rate-limit payload is malformed or a transport-level
    contract is violated. Subclasses :class:`ValueError`."""


@dataclass(frozen=True)
class RateLimitInfo:
    """Normalized view of a Discord 429 (rate limited) response."""

    status: int
    code: Optional[int]
    message: str
    retry_after: float
    global_: bool


def _is_number(value: Any) -> bool:
    """True for int/float (excluding bool) and numeric strings."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return False
        return True
    return False


def _to_float(value: Any) -> float:
    return float(value)


def parse_rate_limit_response(status_code: int, body_dict: Any) -> RateLimitInfo:
    """Parse a Discord 429 response body into a :class:`RateLimitInfo`.

    The expected body shape is::

        {"retry_after": float, "message": str, "global": bool, "code": int}

    Raises:
        TransportError: if the body is not a mapping, is missing a required
            key, or contains a value of the wrong type.
    """
    if not isinstance(body_dict, Mapping):
        raise TransportError(
            f"malformed rate-limit body: expected mapping, got {type(body_dict).__name__}"
        )
    body: Mapping[str, Any] = body_dict

    try:
        retry_after_raw = body["retry_after"]
    except KeyError:
        raise TransportError("malformed rate-limit body: missing 'retry_after'") from None
    if not _is_number(retry_after_raw):
        raise TransportError(
            f"malformed rate-limit body: 'retry_after' must be numeric, got {retry_after_raw!r}"
        )
    retry_after = _to_float(retry_after_raw)
    if retry_after < 0:
        raise TransportError("malformed rate-limit body: 'retry_after' must be >= 0")

    try:
        message = body["message"]
    except KeyError:
        raise TransportError("malformed rate-limit body: missing 'message'") from None
    if not isinstance(message, str):
        raise TransportError(
            f"malformed rate-limit body: 'message' must be str, got {type(message).__name__}"
        )

    try:
        global_ = body["global"]
    except KeyError:
        raise TransportError("malformed rate-limit body: missing 'global'") from None
    if not isinstance(global_, bool):
        raise TransportError(
            f"malformed rate-limit body: 'global' must be bool, got {type(global_).__name__}"
        )

    try:
        code = body["code"]
    except KeyError:
        raise TransportError("malformed rate-limit body: missing 'code'") from None
    if not isinstance(code, int) or isinstance(code, bool):
        raise TransportError(
            f"malformed rate-limit body: 'code' must be int, got {type(code).__name__}"
        )

    return RateLimitInfo(
        status=status_code,
        code=code,
        message=message,
        retry_after=retry_after,
        global_=global_,
    )


_IDEMPOTENT_METHODS = frozenset({"GET", "PUT", "DELETE"})

_MAX_RETRY_AFTER = 60.0


def is_retriable(status_code: int, method: str, retry_after: Any = None) -> bool:
    """Decide whether a failed request may be retried.

    Rules:
        * 429 (rate limited): retriable only when ``retry_after`` is numeric
          and <= 60 seconds.
        * 5xx (server errors): retriable only for idempotent methods
          (GET/PUT/DELETE). Non-idempotent methods such as POST are never
          retried on 5xx.
        * 400/401/403 (client/auth errors): never retriable.
    """
    method = method.upper()
    if status_code == 429:
        return _is_number(retry_after) and _to_float(retry_after) <= _MAX_RETRY_AFTER
    if 500 <= status_code < 600:
        return method in _IDEMPOTENT_METHODS
    return False


class RouteBucket:
    """Per-route 429 cooldown tracker.

    After a 429, :meth:`apply_rate_limit` records a cooldown deadline; the
    route is unavailable until that deadline passes. ``clock`` is injectable
    for deterministic tests; it defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        route: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.route = route
        self.cooldown_until: float = 0.0
        self._clock: Callable[[], float] = clock or time.monotonic

    def apply_rate_limit(self, retry_after: float) -> None:
        """Begin a cooldown that lasts ``retry_after`` seconds."""
        self.cooldown_until = self._clock() + float(retry_after)

    def available(self) -> bool:
        """True when the route is not in a rate-limit cooldown."""
        return self._clock() >= self.cooldown_until

    def reset(self) -> None:
        """Clear any active cooldown."""
        self.cooldown_until = 0.0
