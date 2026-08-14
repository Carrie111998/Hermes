"""Proactive / home / cron delivery targeting for the Discord platform.

Pure logic module: no I/O, no discord.py imports, no config access. All
resolution is fail-closed: a missing or invalid target raises
:class:`ProactiveDeliveryError` instead of silently falling back (see
issue #7206 — no silent origin fallback when home delivery is requested).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

__all__ = [
    "DeliveryTarget",
    "ProactiveDeliveryError",
    "continuable_thread_target",
    "resolve_home_target",
    "resolve_profile_adapter",
]

# Discord snowflakes are non-negative integers that fit in a signed 64-bit
# field (snowflake generation is monotonic and well below 2**63).
_MAX_SNOWFLAKE = (1 << 63) - 1


class ProactiveDeliveryError(ValueError):
    """Raised when a proactive delivery target cannot be resolved.

    Subclasses ``ValueError`` so callers can catch either class.
    """


@dataclass(frozen=True)
class DeliveryTarget:
    """A resolved delivery location: a channel and/or a thread."""

    channel_id: Optional[str]
    thread_id: Optional[str]


def _is_valid_snowflake(value: object) -> bool:
    """Return True when *value* is a snowflake-shaped string.

    A valid snowflake is a string of decimal digits (no sign, no
    whitespace, no separators) whose integer value fits in a signed
    64-bit range. Non-strings are rejected.
    """
    if not isinstance(value, str) or not value.isdigit():
        return False
    try:
        return 0 <= int(value) <= _MAX_SNOWFLAKE
    except ValueError:  # pragma: no cover - isdigit() precludes this
        return False


def resolve_home_target(
    origin_channel: Optional[str],
    home_channel: Optional[str],
    *,
    deliver_home: bool,
) -> DeliveryTarget:
    """Resolve where a proactive message should be delivered.

    When ``deliver_home`` is True the home channel is used and *must* be a
    valid snowflake; a missing home channel raises
    :class:`ProactiveDeliveryError` — there is no silent fallback to the
    origin channel (#7206). When ``deliver_home`` is False the origin
    channel is used; it may be ``None``.
    """
    if deliver_home:
        if home_channel is None:
            raise ProactiveDeliveryError(
                "deliver_home is enabled but no home channel is configured"
            )
        if not _is_valid_snowflake(home_channel):
            raise ProactiveDeliveryError(
                f"home channel is not a valid snowflake: {home_channel!r}"
            )
        return DeliveryTarget(channel_id=home_channel, thread_id=None)

    if origin_channel is not None and not _is_valid_snowflake(origin_channel):
        raise ProactiveDeliveryError(
            f"origin channel is not a valid snowflake: {origin_channel!r}"
        )
    return DeliveryTarget(channel_id=origin_channel, thread_id=None)


def continuable_thread_target(
    thread_id: Optional[str],
    *,
    cron_thread_identity: Optional[str],
) -> DeliveryTarget:
    """Resolve the thread a cron delivery should continue in.

    A live ``thread_id`` wins when it is a valid snowflake. Otherwise the
    ``cron_thread_identity`` recorded for the cron job is used (it is
    validated when used; a broken identity is fail-closed). When neither is
    available the result is ``DeliveryTarget(None, None)`` — no thread.
    """
    if thread_id is not None and _is_valid_snowflake(thread_id):
        return DeliveryTarget(channel_id=None, thread_id=thread_id)

    if cron_thread_identity is not None:
        if not _is_valid_snowflake(cron_thread_identity):
            raise ProactiveDeliveryError(
                "cron thread identity is not a valid snowflake: "
                f"{cron_thread_identity!r}"
            )
        return DeliveryTarget(channel_id=None, thread_id=cron_thread_identity)

    return DeliveryTarget(channel_id=None, thread_id=None)


def resolve_profile_adapter(profile_id: str, adapters: dict) -> str:
    """Resolve the adapter registered for *profile_id*.

    Fail-closed: a missing profile raises :class:`ProactiveDeliveryError`
    rather than silently falling back to a default adapter. *adapters* must
    be a mapping and its resolved value must be a string.
    """
    if not isinstance(adapters, dict):
        raise ProactiveDeliveryError("adapters must be a mapping")
    try:
        adapter = adapters[profile_id]
    except KeyError:
        raise ProactiveDeliveryError(
            f"no delivery adapter registered for profile {profile_id!r}"
        ) from None
    if not isinstance(adapter, str):
        raise ProactiveDeliveryError(
            f"adapter for profile {profile_id!r} is not a string: {adapter!r}"
        )
    return adapter
