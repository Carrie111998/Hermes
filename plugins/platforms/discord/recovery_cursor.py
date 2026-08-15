"""Discord recovery cursor correctness (R3).

Pure logic for tracking per-channel recovery cursors while draining
Discord message history. A cursor records the highest (by numeric
value) message id seen for a channel and whether more pages remain.

Cursors are monotonic: a new ``advance`` for a channel must never move
``last_message_id`` backward. Attempting to do so raises
:class:`RecoveryCursorError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Set


class RecoveryCursorError(ValueError):
    """Raised when a recovery cursor would move backward."""


def _is_snowflake(value: object) -> bool:
    """Return True if *value* is a Discord snowflake (int or digit string)."""
    if isinstance(value, int):
        return True
    return isinstance(value, str) and value.isdigit()


def _validate_snowflake(value: object, *, what: str) -> None:
    if not _is_snowflake(value):
        raise ValueError(
            f"{what} must be a snowflake (int or digit string), got {value!r}"
        )


@dataclass(frozen=True)
class Cursor:
    """Recovery position for a single channel."""

    channel_id: str
    last_message_id: Optional[str]
    has_more: bool


class RecoveryCursorManager:
    """Tracks monotonic recovery cursors and per-channel dedup state."""

    def __init__(self) -> None:
        self._cursors: Dict[str, Cursor] = {}
        self._seen_per_channel: Dict[str, Set[str]] = {}

    def advance(
        self,
        channel_id: str,
        message_ids: List[str],
        *,
        has_more: bool,
    ) -> Cursor:
        """Record the newest batch of message ids for a channel.

        Sets ``last_message_id`` to the numerically largest id in the
        batch. Raises :class:`RecoveryCursorError` if that would move
        the recorded cursor backward.
        """
        _validate_snowflake(channel_id, what="channel_id")
        if not message_ids:
            raise ValueError("message_ids must not be empty")
        for message_id in message_ids:
            _validate_snowflake(message_id, what="message_id")

        new_max = str(max(int(mid) for mid in message_ids))

        prev = self._cursors.get(channel_id)
        if prev is not None and prev.last_message_id is not None:
            prev_max = int(prev.last_message_id)
            if int(new_max) < prev_max:
                raise RecoveryCursorError(
                    f"recovery cursor for channel {channel_id} moved backward: "
                    f"{prev.last_message_id} -> {new_max}"
                )

        cursor = Cursor(
            channel_id=channel_id,
            last_message_id=new_max,
            has_more=has_more,
        )
        self._cursors[channel_id] = cursor
        return cursor

    def dedup(
        self,
        channel_id: str,
        message_ids: List[str],
        seen_ids: Set[str],
    ) -> List[str]:
        """Return the ids in *message_ids* not seen before.

        An id is filtered out if it is already in *seen_ids* (the
        caller's global set) or if it was already seen for this channel
        (the manager's per-channel set). Newly returned ids are added to
        both sets.
        """
        _validate_snowflake(channel_id, what="channel_id")
        channel_seen = self._seen_per_channel.setdefault(channel_id, set())

        result: List[str] = []
        for message_id in message_ids:
            _validate_snowflake(message_id, what="message_id")
            key = str(message_id)
            if key in seen_ids or key in channel_seen:
                continue
            seen_ids.add(key)
            channel_seen.add(key)
            result.append(message_id)
        return result

    def cursor_for(self, channel_id: str) -> Optional[Cursor]:
        """Return the recorded cursor for a channel, or None."""
        return self._cursors.get(channel_id)
