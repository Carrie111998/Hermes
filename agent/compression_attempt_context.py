"""Attempt-local state shared by context-compression call paths."""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator
from typing import Optional

_COMPRESSION_CANCELLED_CHECK: contextvars.ContextVar[
    Optional[Callable[[], bool]]
] = contextvars.ContextVar("hermes_compression_cancelled_check", default=None)


@contextlib.contextmanager
def compression_cancelled_check(check: Callable[[], bool]) -> Iterator[None]:
    """Install the cancellation check for one compression attempt context."""
    token = _COMPRESSION_CANCELLED_CHECK.set(check)
    try:
        yield
    finally:
        _COMPRESSION_CANCELLED_CHECK.reset(token)


def attempt_compression_cancelled() -> Optional[bool]:
    """Return this attempt's cancellation state, or None outside an attempt."""
    check = _COMPRESSION_CANCELLED_CHECK.get()
    if check is None:
        return None
    try:
        return bool(check())
    except Exception:
        return False
