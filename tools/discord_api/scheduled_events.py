"""Discord guild scheduled-event REST request builders.

Pure request-builder module: every function validates its inputs and returns a
request descriptor dict (``method``, ``path``, optional ``json`` body) ready to
be handed to an HTTP client. Aligned with Discord REST API v10
(``resources/guild-scheduled-event.mdx``).
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, Optional

__all__ = [
    "ScheduledEventError",
    "create_scheduled_event_request",
    "edit_scheduled_event_request",
    "delete_scheduled_event_request",
    "list_scheduled_events_request",
]

# Discord snowflakes are unsigned 64-bit integers; real snowflakes fit in 63 bits.
_SNOWFLAKE_MAX = 2**63 - 1

# Guild Scheduled Event entity types (REST v10).
ENTITY_TYPE_EXTERNAL = 1
ENTITY_TYPE_STAGE = 2
ENTITY_TYPE_VOICE = 3
_ENTITY_TYPES = (ENTITY_TYPE_EXTERNAL, ENTITY_TYPE_STAGE, ENTITY_TYPE_VOICE)

_NAME_MIN = 1
_NAME_MAX = 100
_DESCRIPTION_MAX = 1000
# GUILD_ONLY is currently the only supported privacy level.
_PRIVACY_LEVEL = 2

# Loose ISO-8601 shape; full semantic validation is delegated to
# datetime.fromisoformat (with ``Z`` normalized to ``+00:00``).
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


class ScheduledEventError(ValueError):
    """Raised when a scheduled-event request cannot be validated."""


def _validate_snowflake(value, field: str) -> int:
    """Validate a Discord snowflake (int or numeric string); return the int."""
    if isinstance(value, bool):
        raise ScheduledEventError(f"{field} must be a valid snowflake, got {value!r}")
    if isinstance(value, int):
        num = value
    elif isinstance(value, str):
        if not value.isdigit():
            raise ScheduledEventError(f"{field} must be a valid snowflake, got {value!r}")
        num = int(value)
    else:
        raise ScheduledEventError(f"{field} must be a valid snowflake, got {value!r}")
    if num < 0 or num > _SNOWFLAKE_MAX:
        raise ScheduledEventError(f"{field} out of range for a Discord snowflake: {num}")
    return num


def _validate_iso8601(value, field: str) -> str:
    """Validate an ISO-8601 timestamp string; return it unchanged."""
    if not isinstance(value, str):
        raise ScheduledEventError(f"{field} must be an ISO-8601 string, got {value!r}")
    if not _ISO8601_RE.match(value):
        raise ScheduledEventError(f"{field} must be an ISO-8601 string, got {value!r}")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        _dt.datetime.fromisoformat(normalized)
    except ValueError:
        raise ScheduledEventError(
            f"{field} must be a valid ISO-8601 timestamp, got {value!r}"
        ) from None
    return value


def _descriptor(
    method: str, path: str, json_body: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    descriptor: Dict[str, Any] = {"method": method, "path": path}
    if json_body:
        descriptor["json"] = json_body
    return descriptor


def create_scheduled_event_request(
    guild_id,
    *,
    name,
    scheduled_start_time,
    channel_id=None,
    entity_type=ENTITY_TYPE_EXTERNAL,
    description=None,
    privacy_level=_PRIVACY_LEVEL,
    scheduled_end_time=None,
) -> Dict[str, Any]:
    """Build a validated POST /guilds/{guild}/scheduled-events request.

    Entity-type rules (REST v10):
    - EXTERNAL (1): ``channel_id`` must be omitted and ``scheduled_end_time``
      is required.
    - STAGE_INSTANCE (2) / VOICE (3): ``channel_id`` refers to the stage/voice
      channel the event is held in.
    """
    guild = _validate_snowflake(guild_id, "guild_id")

    if not isinstance(name, str):
        raise ScheduledEventError(f"name must be a string, got {name!r}")
    if not (_NAME_MIN <= len(name) <= _NAME_MAX):
        raise ScheduledEventError(
            f"name must be between {_NAME_MIN} and {_NAME_MAX} characters"
        )

    start = _validate_iso8601(scheduled_start_time, "scheduled_start_time")

    if entity_type not in _ENTITY_TYPES:
        raise ScheduledEventError(
            f"entity_type must be one of {_ENTITY_TYPES}, got {entity_type!r}"
        )

    if privacy_level != _PRIVACY_LEVEL:
        raise ScheduledEventError(
            f"privacy_level must be {_PRIVACY_LEVEL} (GUILD_ONLY), got {privacy_level!r}"
        )

    if channel_id is not None:
        channel_id = _validate_snowflake(channel_id, "channel_id")

    if description is not None:
        if not isinstance(description, str):
            raise ScheduledEventError(f"description must be a string, got {description!r}")
        if len(description) > _DESCRIPTION_MAX:
            raise ScheduledEventError(
                f"description must be at most {_DESCRIPTION_MAX} characters"
            )

    end = None
    if scheduled_end_time is not None:
        end = _validate_iso8601(scheduled_end_time, "scheduled_end_time")

    if entity_type == ENTITY_TYPE_EXTERNAL:
        if channel_id is not None:
            raise ScheduledEventError("external scheduled events cannot set channel_id")
        if end is None:
            raise ScheduledEventError(
                "external scheduled events require scheduled_end_time"
            )

    body: Dict[str, Any] = {
        "name": name,
        "scheduled_start_time": start,
        "entity_type": entity_type,
        "privacy_level": privacy_level,
    }
    if channel_id is not None:
        body["channel_id"] = str(channel_id)
    if description is not None:
        body["description"] = description
    if end is not None:
        body["scheduled_end_time"] = end

    return _descriptor("POST", f"/guilds/{guild}/scheduled-events", body)


def edit_scheduled_event_request(guild_id, event_id, **fields) -> Dict[str, Any]:
    """Build a validated PATCH /guilds/{guild}/scheduled-events/{event} request.

    Only the fields explicitly provided (including ``None`` values, which clear
    a field) are included in the body. Unknown field names are rejected.
    """
    guild = _validate_snowflake(guild_id, "guild_id")
    event = _validate_snowflake(event_id, "event_id")

    body: Dict[str, Any] = {}
    for key, value in fields.items():
        if key == "name":
            if not isinstance(value, str):
                raise ScheduledEventError("name must be a string")
            if not (_NAME_MIN <= len(value) <= _NAME_MAX):
                raise ScheduledEventError(
                    f"name must be between {_NAME_MIN} and {_NAME_MAX} characters"
                )
        elif key == "description":
            if not isinstance(value, str):
                raise ScheduledEventError("description must be a string")
            if len(value) > _DESCRIPTION_MAX:
                raise ScheduledEventError(
                    f"description must be at most {_DESCRIPTION_MAX} characters"
                )
        elif key == "channel_id":
            if value is not None:
                value = str(_validate_snowflake(value, "channel_id"))
        elif key == "entity_type":
            if value not in _ENTITY_TYPES:
                raise ScheduledEventError(
                    f"entity_type must be one of {_ENTITY_TYPES}, got {value!r}"
                )
        elif key == "privacy_level":
            if value != _PRIVACY_LEVEL:
                raise ScheduledEventError(
                    f"privacy_level must be {_PRIVACY_LEVEL} (GUILD_ONLY), got {value!r}"
                )
        elif key == "scheduled_start_time":
            value = _validate_iso8601(value, "scheduled_start_time")
        elif key == "scheduled_end_time":
            if value is not None:
                value = _validate_iso8601(value, "scheduled_end_time")
        else:
            raise ScheduledEventError(f"unsupported scheduled-event field: {key!r}")
        body[key] = value

    # Cross-field consistency for the fields provided in this call only.
    if (
        body.get("entity_type") == ENTITY_TYPE_EXTERNAL
        and "channel_id" in body
        and body["channel_id"] is not None
    ):
        raise ScheduledEventError("external scheduled events cannot set channel_id")

    return _descriptor("PATCH", f"/guilds/{guild}/scheduled-events/{event}", body)


def delete_scheduled_event_request(guild_id, event_id) -> Dict[str, Any]:
    """Build a validated DELETE /guilds/{guild}/scheduled-events/{event} request."""
    guild = _validate_snowflake(guild_id, "guild_id")
    event = _validate_snowflake(event_id, "event_id")
    return _descriptor("DELETE", f"/guilds/{guild}/scheduled-events/{event}")


def list_scheduled_events_request(guild_id) -> Dict[str, Any]:
    """Build a validated GET /guilds/{guild}/scheduled-events request."""
    guild = _validate_snowflake(guild_id, "guild_id")
    return _descriptor("GET", f"/guilds/{guild}/scheduled-events")
