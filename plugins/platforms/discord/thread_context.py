"""Discord thread session isolation and history partition (pure logic, no I/O).

Feature T2: each (channel_id, thread_id) pair owns an isolated session key so a
thread's conversation never bleeds into the parent channel's, and history is
partitioned so thread messages are not double-counted in parent history.
"""

from __future__ import annotations

from typing import Dict, List, Optional


class ThreadContextError(ValueError):
    """Raised when thread-context input is invalid."""


_SNOWFLAKE_MAX = 2**63 - 1


def _validate_snowflake(value, name: str) -> str:
    """Validate a Discord snowflake id; return its normalized string form."""
    if isinstance(value, bool):
        raise ThreadContextError(f"{name} must be a snowflake id, got {value!r}")
    if isinstance(value, int):
        number = value
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or not text.isdigit():
            raise ThreadContextError(
                f"{name} must be a numeric snowflake id, got {value!r}"
            )
        number = int(text)
    else:
        raise ThreadContextError(
            f"{name} must be an int or str snowflake id, got {value!r}"
        )
    if number < 0 or number > _SNOWFLAKE_MAX:
        raise ThreadContextError(f"{name} out of snowflake range: {value!r}")
    return text


class ThreadSessionRegistry:
    """Tracks which session id owns each (channel_id, thread_id) pair."""

    def __init__(self) -> None:
        self._sessions: Dict[str, str] = {}

    def key_for(self, channel_id, thread_id) -> str:
        """Return the stable session key for a channel/thread pair."""
        channel = _validate_snowflake(channel_id, "channel_id")
        thread = _validate_snowflake(thread_id, "thread_id")
        return f"{channel}:{thread}"

    def is_isolated(self, key_a: str, key_b: str) -> bool:
        """True when the two keys belong to different sessions.

        Different thread or different channel => different session => isolated.
        """
        if not isinstance(key_a, str) or not isinstance(key_b, str):
            raise ThreadContextError("is_isolated expects string session keys")
        return key_a != key_b

    def register(self, channel_id, thread_id, session_id: str) -> str:
        """Associate a session id with a channel/thread pair; return its key."""
        if not isinstance(session_id, str) or not session_id.strip():
            raise ThreadContextError(
                f"session_id must be a non-empty string, got {session_id!r}"
            )
        key = self.key_for(channel_id, thread_id)
        self._sessions[key] = session_id
        return key

    def session_for(self, channel_id, thread_id) -> Optional[str]:
        """Return the session id registered for a channel/thread pair, if any."""
        return self._sessions.get(self.key_for(channel_id, thread_id))

    def unregister(self, channel_id, thread_id) -> None:
        """Remove a channel/thread pair's session mapping (idempotent)."""
        self._sessions.pop(self.key_for(channel_id, thread_id), None)


def _validate_message(message, name: str) -> str:
    """Validate a history message dict; return its normalized id."""
    if not isinstance(message, dict):
        raise ThreadContextError(
            f"{name} items must be message dicts, got {message!r}"
        )
    if "id" not in message:
        raise ThreadContextError(
            f"{name} items must contain an 'id', got {message!r}"
        )
    return _validate_snowflake(message["id"], f"{name} item id")


class HistoryPartition:
    """Separates parent-channel history from a thread's own history."""

    def partition(
        self, parent_messages: list, thread_messages: list, *, thread_id
    ) -> Dict[str, list]:
        """Split history into {'parent': [...], 'thread': [...]}.

        A message id present in both lists is placed ONLY in 'thread' so
        thread replies that also appear in the parent channel are not
        double-counted.
        """
        _validate_snowflake(thread_id, "thread_id")
        if not isinstance(parent_messages, list):
            raise ThreadContextError("parent_messages must be a list")
        if not isinstance(thread_messages, list):
            raise ThreadContextError("thread_messages must be a list")

        thread_ids = {
            _validate_message(message, "thread_messages") for message in thread_messages
        }
        parent = [
            message
            for message in parent_messages
            if _validate_message(message, "parent_messages") not in thread_ids
        ]
        return {"parent": parent, "thread": list(thread_messages)}
