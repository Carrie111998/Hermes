"""Discord forum starter/tag REST request builders (REST v10).

Aligned with discord.com/developers/docs/resources/channel (forum channels).
Pure request-builder module: validates inputs and returns validated request
descriptors (``{"method", "path", "payload"}``); it performs no network I/O.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

__all__ = [
    "ForumError",
    "FORUM_CHANNEL_TYPE",
    "MAX_APPLIED_TAGS",
    "NAME_MAX_LENGTH",
    "NAME_MIN_LENGTH",
    "create_forum_post_request",
    "list_forum_tags_request",
    "set_forum_post_tags_request",
]

FORUM_CHANNEL_TYPE = 11
MAX_APPLIED_TAGS = 5
NAME_MIN_LENGTH = 1
NAME_MAX_LENGTH = 100


class ForumError(ValueError):
    """Raised when a forum request descriptor fails validation."""


def _snowflake(value: Any, field: str) -> str:
    """Validate a Discord snowflake and normalize it to its canonical string form."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ForumError(f"{field} must be a snowflake, got {value!r}")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ForumError(
            f"{field} must be a snowflake (int or str), got {type(value).__name__}"
        )
    if not text or not text.isdigit():
        raise ForumError(f"{field} must be a snowflake, got {value!r}")
    return text


def _applied_tags(value: Optional[List[Any]]) -> Optional[List[str]]:
    """Validate an applied_tags list; ``None`` is the omit-the-key sentinel."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ForumError("applied_tags must be a list of snowflakes")
    if len(value) > MAX_APPLIED_TAGS:
        raise ForumError(
            f"applied_tags may contain at most {MAX_APPLIED_TAGS} tags, got {len(value)}"
        )
    return [_snowflake(tag, f"applied_tags[{i}]") for i, tag in enumerate(value)]


def create_forum_post_request(
    channel_id: Any,
    *,
    name: str,
    message_content: Optional[str] = None,
    applied_tags: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Build the POST /channels/{channel}/threads request that starts a forum post.

    Args:
        channel_id: Snowflake of the forum channel.
        name: Post title, 1..100 characters.
        message_content: Optional initial message content.
        applied_tags: Optional list of tag snowflakes, max 5.

    Returns:
        A request descriptor dict: ``{"method": "POST", "path": ..., "payload": ...}``.

    Raises:
        ForumError: on any validation violation.
    """
    if not isinstance(name, str):
        raise ForumError(f"name must be a str, got {type(name).__name__}")
    if not (NAME_MIN_LENGTH <= len(name) <= NAME_MAX_LENGTH):
        raise ForumError(
            f"name must be {NAME_MIN_LENGTH}..{NAME_MAX_LENGTH} characters, got {len(name)}"
        )
    if message_content is not None and not isinstance(message_content, str):
        raise ForumError(
            f"message_content must be a str or None, got {type(message_content).__name__}"
        )

    channel = _snowflake(channel_id, "channel_id")
    tags = _applied_tags(applied_tags)

    payload: Dict[str, Any] = {"name": name, "type": FORUM_CHANNEL_TYPE}
    if message_content is not None:
        payload["message"] = {"content": message_content}
    if tags is not None:
        payload["applied_tags"] = tags

    return {"method": "POST", "path": f"/channels/{channel}/threads", "payload": payload}


def list_forum_tags_request(channel_id: Any) -> Dict[str, Any]:
    """Build the GET /channels/{channel}/tags request listing a forum's tags."""
    channel = _snowflake(channel_id, "channel_id")
    return {"method": "GET", "path": f"/channels/{channel}/tags", "payload": None}


def set_forum_post_tags_request(
    thread_id: Any, tag_ids: List[Any]
) -> Dict[str, Any]:
    """Build the PATCH /channels/{thread} request setting a forum post's tags.

    Args:
        thread_id: Snowflake of the forum post (thread).
        tag_ids: Tag snowflakes to apply; empty list clears tags. Max 5.

    Returns:
        A request descriptor dict: ``{"method": "PATCH", "path": ..., "payload": ...}``.
    """
    thread = _snowflake(thread_id, "thread_id")
    tags = _applied_tags(tag_ids)
    if tags is None:
        raise ForumError("tag_ids is required")
    return {
        "method": "PATCH",
        "path": f"/channels/{thread}",
        "payload": {"applied_tags": tags},
    }
