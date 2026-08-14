"""Discord moderation REST request builders (REST v10).

Pure request-builder helpers: each function validates its inputs and returns
a plain descriptor dict an HTTP client can execute:

    {
        "method": "PATCH",
        "path": "/guilds/<guild_id>/members/<user_id>",
        "payload": {...},   # JSON body (present when applicable)
        "params": {...},    # query-string params (present when applicable)
        "headers": {...},   # extra headers such as X-Audit-Log-Reason
    }

No network I/O happens here. Paths and payload shapes follow Discord REST v10
moderation endpoints (Modify Guild Member, Remove Guild Member, Create/Remove
Guild Ban). Invalid input raises :class:`ModerationError` (a ValueError
subclass) instead of producing a malformed request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "MAX_DELETE_MESSAGE_DAYS",
    "MAX_REASON_LENGTH",
    "MAX_TIMEOUT_SECONDS",
    "ModerationError",
    "ban_member_request",
    "kick_member_request",
    "remove_timeout_request",
    "timeout_member_request",
    "unban_member_request",
]

#: Maximum audit-log reason length accepted by Discord.
MAX_REASON_LENGTH = 512
#: Maximum timeout duration in seconds (Discord caps timeouts at 28 days).
MAX_TIMEOUT_SECONDS = 2419200
#: Discord only accepts delete_message_days in the range 0..7.
MAX_DELETE_MESSAGE_DAYS = 7


class ModerationError(ValueError):
    """Raised when a moderation request descriptor cannot be built."""


def _snowflake(value: object, name: str) -> str:
    """Validate and canonicalize a Discord snowflake ID."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ModerationError(
            f"{name} must be a snowflake (int or str), got {type(value).__name__}"
        )
    text = str(value)
    if not text.isdigit():
        raise ModerationError(f"{name} must be a numeric snowflake, got {value!r}")
    if int(text) <= 0:
        raise ModerationError(f"{name} must be a positive snowflake, got {value!r}")
    return text


def _reason(reason: object) -> str | None:
    """Validate an audit-log reason (None or a str of at most 512 chars)."""
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise ModerationError(
            f"reason must be a string, got {type(reason).__name__}"
        )
    if len(reason) > MAX_REASON_LENGTH:
        raise ModerationError(
            f"reason must be at most {MAX_REASON_LENGTH} characters, got {len(reason)}"
        )
    return reason


def _iso8601(dt: datetime) -> str:
    """Format a datetime as an ISO8601 UTC timestamp with a ``Z`` suffix."""
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _descriptor(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
) -> dict:
    """Assemble a request descriptor, omitting empty optional sections."""
    desc: dict = {"method": method, "path": path}
    if payload is not None:
        desc["payload"] = payload
    if params:
        desc["params"] = params
    if headers:
        desc["headers"] = headers
    return desc


def timeout_member_request(
    guild_id: object,
    user_id: object,
    *,
    duration_seconds: int,
    reason: str | None = None,
) -> dict:
    """Build a PATCH Modify Guild Member request timing a member out.

    ``communication_disabled_until`` is set to the current UTC time plus
    ``duration_seconds`` (1..2419200 seconds, i.e. up to 28 days).
    """
    guild = _snowflake(guild_id, "guild_id")
    user = _snowflake(user_id, "user_id")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int):
        raise ModerationError(
            f"duration_seconds must be an int, got {type(duration_seconds).__name__}"
        )
    if not 1 <= duration_seconds <= MAX_TIMEOUT_SECONDS:
        raise ModerationError(
            f"duration_seconds must be between 1 and {MAX_TIMEOUT_SECONDS}, "
            f"got {duration_seconds}"
        )
    reason = _reason(reason)
    until = _iso8601(datetime.now(timezone.utc) + timedelta(seconds=duration_seconds))
    return _descriptor(
        "PATCH",
        f"/guilds/{guild}/members/{user}",
        payload={"communication_disabled_until": until},
        headers={"X-Audit-Log-Reason": reason} if reason is not None else None,
    )


def remove_timeout_request(guild_id: object, user_id: object) -> dict:
    """Build a PATCH Modify Guild Member request clearing a timeout."""
    guild = _snowflake(guild_id, "guild_id")
    user = _snowflake(user_id, "user_id")
    return _descriptor(
        "PATCH",
        f"/guilds/{guild}/members/{user}",
        payload={"communication_disabled_until": None},
    )


def kick_member_request(
    guild_id: object, user_id: object, *, reason: str | None = None
) -> dict:
    """Build a DELETE Remove Guild Member request.

    Discord passes the audit-log reason for DELETE requests as the
    ``reason`` query parameter.
    """
    guild = _snowflake(guild_id, "guild_id")
    user = _snowflake(user_id, "user_id")
    reason = _reason(reason)
    return _descriptor(
        "DELETE",
        f"/guilds/{guild}/members/{user}",
        params={"reason": reason} if reason is not None else None,
    )


def ban_member_request(
    guild_id: object,
    user_id: object,
    *,
    delete_message_days: int = 0,
    reason: str | None = None,
) -> dict:
    """Build a PUT Create Guild Ban request.

    ``delete_message_days`` (0..7) controls how many days of the member's
    recent messages are deleted; ``reason`` is included in the JSON body.
    """
    guild = _snowflake(guild_id, "guild_id")
    user = _snowflake(user_id, "user_id")
    if isinstance(delete_message_days, bool) or not isinstance(delete_message_days, int):
        raise ModerationError(
            f"delete_message_days must be an int, got {type(delete_message_days).__name__}"
        )
    if not 0 <= delete_message_days <= MAX_DELETE_MESSAGE_DAYS:
        raise ModerationError(
            f"delete_message_days must be between 0 and {MAX_DELETE_MESSAGE_DAYS}, "
            f"got {delete_message_days}"
        )
    reason = _reason(reason)
    payload: dict = {"delete_message_days": delete_message_days}
    if reason is not None:
        payload["reason"] = reason
    return _descriptor("PUT", f"/guilds/{guild}/bans/{user}", payload=payload)


def unban_member_request(guild_id: object, user_id: object) -> dict:
    """Build a DELETE Remove Guild Ban request."""
    guild = _snowflake(guild_id, "guild_id")
    user = _snowflake(user_id, "user_id")
    return _descriptor("DELETE", f"/guilds/{guild}/bans/{user}")
