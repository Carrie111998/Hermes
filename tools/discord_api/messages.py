"""Discord REST v10 message request builders (feature M2).

Pure request-descriptor builders for Discord message edit/delete REST actions.
No network calls are made here; every function returns a validated descriptor
of the shape::

    {"method": str, "path": str, "payload": dict, "query": dict}

Snowflake IDs are validated as all-digit strings. Invalid input raises
:class:`MessageError`, a subclass of :exc:`ValueError`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "MessageError",
    "MAX_CONTENT_LENGTH",
    "BULK_DELETE_MIN",
    "BULK_DELETE_MAX",
    "edit_message_request",
    "delete_message_request",
    "delete_messages_bulk_request",
]

MAX_CONTENT_LENGTH = 2000
BULK_DELETE_MIN = 2
BULK_DELETE_MAX = 100

_ASCII_DIGITS = frozenset("0123456789")


class MessageError(ValueError):
    """Raised when a message request descriptor cannot be validated."""


def _validate_snowflake(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise MessageError(
            f"{name} must be an all-digit string, got {type(value).__name__}"
        )
    if not value:
        raise MessageError(f"{name} must not be empty")
    if any(char not in _ASCII_DIGITS for char in value):
        raise MessageError(f"{name} must be an all-digit snowflake, got {value!r}")
    return value


def edit_message_request(
    channel_id: str,
    message_id: str,
    *,
    content: Optional[str] = None,
    embeds: Optional[List[Dict[str, Any]]] = None,
    flags: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a PATCH /channels/{channel}/messages/{message} request descriptor.

    Any of ``content``, ``embeds``, or ``flags`` may be provided; the payload
    only contains the fields the caller supplied. ``content`` is limited to
    2000 characters per the Discord REST v10 contract.
    """
    channel_id = _validate_snowflake(channel_id, name="channel_id")
    message_id = _validate_snowflake(message_id, name="message_id")

    payload: Dict[str, Any] = {}
    if content is not None:
        if not isinstance(content, str):
            raise MessageError(
                f"content must be a string, got {type(content).__name__}"
            )
        if len(content) > MAX_CONTENT_LENGTH:
            raise MessageError(
                f"content must be at most {MAX_CONTENT_LENGTH} characters, "
                f"got {len(content)}"
            )
        payload["content"] = content
    if embeds is not None:
        if not isinstance(embeds, list):
            raise MessageError(
                f"embeds must be a list, got {type(embeds).__name__}"
            )
        payload["embeds"] = embeds
    if flags is not None:
        if not isinstance(flags, int):
            raise MessageError(
                f"flags must be an int, got {type(flags).__name__}"
            )
        payload["flags"] = flags

    return {
        "method": "PATCH",
        "path": f"/channels/{channel_id}/messages/{message_id}",
        "payload": payload,
        "query": {},
    }


def delete_message_request(channel_id: str, message_id: str) -> Dict[str, Any]:
    """Build a DELETE /channels/{channel}/messages/{message} request descriptor."""
    channel_id = _validate_snowflake(channel_id, name="channel_id")
    message_id = _validate_snowflake(message_id, name="message_id")
    return {
        "method": "DELETE",
        "path": f"/channels/{channel_id}/messages/{message_id}",
        "payload": {},
        "query": {},
    }


def delete_messages_bulk_request(
    channel_id: str, message_ids: List[str]
) -> Dict[str, Any]:
    """Build a POST /channels/{channel}/messages/bulk-delete request descriptor.

    ``message_ids`` must contain between 2 and 100 all-digit snowflake strings
    (inclusive), matching the Discord REST v10 bulk-delete endpoint contract.
    """
    channel_id = _validate_snowflake(channel_id, name="channel_id")
    if not isinstance(message_ids, (list, tuple)):
        raise MessageError(
            "message_ids must be a list of snowflake strings, got "
            f"{type(message_ids).__name__}"
        )
    message_ids = list(message_ids)
    if not (BULK_DELETE_MIN <= len(message_ids) <= BULK_DELETE_MAX):
        raise MessageError(
            f"message_ids must contain between {BULK_DELETE_MIN} and "
            f"{BULK_DELETE_MAX} ids, got {len(message_ids)}"
        )
    validated = [
        _validate_snowflake(message_id, name="message_ids entry")
        for message_id in message_ids
    ]
    return {
        "method": "POST",
        "path": f"/channels/{channel_id}/messages/bulk-delete",
        "payload": {"messages": validated},
        "query": {},
    }
