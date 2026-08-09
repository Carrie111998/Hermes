"""Explicit tombstones for removed messaging-platform identifiers.

These identifiers are accepted only while reading historical data so upgrades can
retire it safely.  They must never be registered, routed, or reported as active
runtime platforms.
"""

from __future__ import annotations

from typing import Any


RETIRED_PLATFORM_IDS = frozenset({"photon"})


def is_retired_platform_id(value: Any) -> bool:
    """Return whether *value* names a permanently retired platform."""
    return isinstance(value, str) and value.strip().lower() in RETIRED_PLATFORM_IDS
