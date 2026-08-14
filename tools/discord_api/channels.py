"""Discord channel/category CRUD REST request builders (REST API v10).

Pure request-descriptor builders aligned with the Discord REST API v10
``resources/channel.mdx`` (Create Guild Channel, Modify Channel, Delete
Channel) and ``resources/guild.mdx`` snowflake semantics. No network I/O;
each function validates its inputs and returns a plain descriptor dict::

    {"method": "POST", "path": "/guilds/{guild_id}/channels", "json": {...}}

Paths use the validated, normalized snowflake as a string. ``json`` is
``None`` for requests that carry no body.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Discord snowflakes are unsigned 64-bit integers; Discord reserves the top
# bit, so the valid range is 0 .. 2**63 - 1.
_MAX_SNOWFLAKE = 2**63 - 1


class ChannelError(ValueError):
    """Raised when a channel request descriptor cannot be validated."""


def _validate_snowflake(value: Any, field: str) -> str:
    """Return *value* normalized to a digit string, or raise ``ChannelError``."""
    if isinstance(value, bool):
        raise ChannelError(f"{field} must be a snowflake, got bool")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ChannelError(
            f"{field} must be a snowflake (integer or digit string), "
            f"got {type(value).__name__}"
        )
    if not text or not text.isdigit():
        raise ChannelError(f"{field} must be a snowflake of digits only, got {value!r}")
    if len(text) > 20 or int(text) > _MAX_SNOWFLAKE:
        raise ChannelError(
            f"{field} out of snowflake range (0..{_MAX_SNOWFLAKE}), got {value!r}"
        )
    return text


def _validate_name(name: Any, field: str = "name") -> str:
    """Trim and validate a channel name (1..100 chars)."""
    if not isinstance(name, str):
        raise ChannelError(f"{field} must be a string, got {type(name).__name__}")
    trimmed = name.strip()
    if not trimmed:
        raise ChannelError(f"{field} must be 1-100 characters after trimming, got empty")
    if len(trimmed) > 100:
        raise ChannelError(
            f"{field} must be at most 100 characters, got {len(trimmed)}"
        )
    return trimmed


def _validate_topic(topic: Any) -> str:
    if not isinstance(topic, str):
        raise ChannelError(f"topic must be a string, got {type(topic).__name__}")
    if len(topic) > 1024:
        raise ChannelError(f"topic must be at most 1024 characters, got {len(topic)}")
    return topic


def _validate_type(channel_type: Any) -> int:
    if isinstance(channel_type, bool) or not isinstance(channel_type, int):
        raise ChannelError(
            f"type must be an integer channel type, got {type(channel_type).__name__}"
        )
    return channel_type


def _validate_nsfw(nsfw: Any) -> bool:
    if not isinstance(nsfw, bool):
        raise ChannelError(f"nsfw must be a bool, got {type(nsfw).__name__}")
    return nsfw


def _validate_rate_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChannelError(
            f"rate_limit_per_user must be an integer, got {type(value).__name__}"
        )
    if not 0 <= value <= 21600:
        raise ChannelError(
            f"rate_limit_per_user must be between 0 and 21600, got {value}"
        )
    return value


def create_channel_request(
    guild_id: Any,
    *,
    name: str,
    type: int = 0,
    topic: Optional[str] = None,
    nsfw: bool = False,
    rate_limit_per_user: int = 0,
    parent_id: Any = None,
) -> Dict[str, Any]:
    """Build a Create Guild Channel request (POST /guilds/{guild}/channels)."""
    gid = _validate_snowflake(guild_id, "guild_id")
    payload: Dict[str, Any] = {
        "name": _validate_name(name),
        "type": _validate_type(type),
        "nsfw": _validate_nsfw(nsfw),
        "rate_limit_per_user": _validate_rate_limit(rate_limit_per_user),
    }
    if topic is not None:
        payload["topic"] = _validate_topic(topic)
    if parent_id is not None:
        payload["parent_id"] = _validate_snowflake(parent_id, "parent_id")
    return {"method": "POST", "path": f"/guilds/{gid}/channels", "json": payload}


def edit_channel_request(
    channel_id: Any,
    *,
    name: Optional[str] = None,
    topic: Optional[str] = None,
    nsfw: Optional[bool] = None,
    parent_id: Any = None,
) -> Dict[str, Any]:
    """Build a Modify Channel request (PATCH /channels/{channel}).

    Only explicitly provided fields are included in the payload; ``None``
    means "leave unchanged". At least one field must be provided.
    """
    cid = _validate_snowflake(channel_id, "channel_id")
    payload: Dict[str, Any] = {}
    if name is not None:
        payload["name"] = _validate_name(name)
    if topic is not None:
        payload["topic"] = _validate_topic(topic)
    if nsfw is not None:
        payload["nsfw"] = _validate_nsfw(nsfw)
    if parent_id is not None:
        payload["parent_id"] = _validate_snowflake(parent_id, "parent_id")
    if not payload:
        raise ChannelError(
            "edit_channel_request requires at least one field to modify"
        )
    return {"method": "PATCH", "path": f"/channels/{cid}", "json": payload}


def delete_channel_request(channel_id: Any) -> Dict[str, Any]:
    """Build a Delete Channel request (DELETE /channels/{channel})."""
    cid = _validate_snowflake(channel_id, "channel_id")
    return {"method": "DELETE", "path": f"/channels/{cid}", "json": None}
