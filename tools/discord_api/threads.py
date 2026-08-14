"""Discord REST API v10 thread lifecycle request builders.

Pure request-descriptor builders aligned with the Discord REST API v10
``topics/threads`` documentation.  Each builder validates its inputs and
returns a descriptor dict with exactly four keys::

    {"method": str, "path": str, "payload": dict | None, "query": dict}

No network I/O is performed; descriptors are meant to be executed by a
transport layer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

SNOWFLAKE_MAX = (1 << 63) - 1

# Thread types (REST v10, topics/threads.mdx).
THREAD_TYPE_NEWS = 10  # Announcement Thread (news channel)
THREAD_TYPE_PUBLIC = 11  # Public Thread
THREAD_TYPE_PRIVATE = 12  # Private Thread
VALID_THREAD_TYPES = (THREAD_TYPE_NEWS, THREAD_TYPE_PUBLIC, THREAD_TYPE_PRIVATE)

NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 100


class ThreadError(ValueError):
    """Raised when a thread request cannot be built from invalid input."""


def _validate_snowflake(value: Union[int, str], field: str) -> str:
    """Validate ``value`` is a Discord snowflake and return it as a string."""
    if value is None or isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ThreadError(f"{field} must be a valid snowflake, got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            raise ThreadError(f"{field} must be a valid snowflake, got {value!r}")
        snowflake = int(text)
    else:
        snowflake = value
    if snowflake < 0 or snowflake > SNOWFLAKE_MAX:
        raise ThreadError(
            f"{field} must be a snowflake in [0, {SNOWFLAKE_MAX}], got {value!r}"
        )
    return str(snowflake)


def _validate_name(name: str, field: str = "name") -> str:
    """Validate and trim a thread name (1..100 chars after trimming)."""
    if not isinstance(name, str):
        raise ThreadError(f"{field} must be a string, got {type(name).__name__}")
    trimmed = name.strip()
    if not (NAME_MIN_LENGTH <= len(trimmed) <= NAME_MAX_LENGTH):
        raise ThreadError(
            f"{field} must be {NAME_MIN_LENGTH}-{NAME_MAX_LENGTH} characters "
            f"after trimming, got {len(trimmed)}"
        )
    return trimmed


def start_thread_request(
    channel_id: Union[int, str],
    *,
    name: str,
    message_id: Optional[Union[int, str]] = None,
    type: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a request to start a thread.

    Without ``message_id``: POST ``/channels/{channel}/threads`` (public or
    news thread).  With ``message_id``: POST
    ``/channels/{channel}/messages/{message}/threads`` (thread from an
    existing message).  ``type`` may be 10 (news), 11 (public) or 12
    (private); when omitted it is left out of the payload.
    """
    channel = _validate_snowflake(channel_id, "channel_id")
    trimmed = _validate_name(name)
    payload: Dict[str, Any] = {"name": trimmed}
    if type is not None:
        if (
            not isinstance(type, int)
            or isinstance(type, bool)
            or type not in VALID_THREAD_TYPES
        ):
            raise ThreadError(
                f"type must be one of {VALID_THREAD_TYPES}, got {type!r}"
            )
        payload["type"] = type
    if message_id is not None:
        message = _validate_snowflake(message_id, "message_id")
        path = f"/channels/{channel}/messages/{message}/threads"
    else:
        path = f"/channels/{channel}/threads"
    return {"method": "POST", "path": path, "payload": payload, "query": {}}


def rename_thread_request(
    thread_id: Union[int, str], name: str
) -> Dict[str, Any]:
    """Build a request to rename a thread (PATCH ``/channels/{thread}``)."""
    thread = _validate_snowflake(thread_id, "thread_id")
    trimmed = _validate_name(name)
    return {
        "method": "PATCH",
        "path": f"/channels/{thread}",
        "payload": {"name": trimmed},
        "query": {},
    }


def archive_thread_request(
    thread_id: Union[int, str],
    archived: bool = True,
    locked: bool = False,
) -> Dict[str, Any]:
    """Build a request to archive/unarchive (and optionally lock) a thread.

    PATCH ``/channels/{thread}`` with payload
    ``{"archived": bool, "locked": bool}``.
    """
    thread = _validate_snowflake(thread_id, "thread_id")
    return {
        "method": "PATCH",
        "path": f"/channels/{thread}",
        "payload": {"archived": bool(archived), "locked": bool(locked)},
        "query": {},
    }


def list_active_threads_request(channel_id: Union[int, str]) -> Dict[str, Any]:
    """Build a request to list a channel's active threads.

    GET ``/channels/{channel}/threads/active``.
    """
    channel = _validate_snowflake(channel_id, "channel_id")
    return {
        "method": "GET",
        "path": f"/channels/{channel}/threads/active",
        "payload": None,
        "query": {},
    }


def join_thread_request(thread_id: Union[int, str]) -> Dict[str, Any]:
    """Build a request to join a thread as the current user.

    PUT ``/channels/{thread}/thread-members/@me``.
    """
    thread = _validate_snowflake(thread_id, "thread_id")
    return {
        "method": "PUT",
        "path": f"/channels/{thread}/thread-members/@me",
        "payload": None,
        "query": {},
    }
