"""Channel-scoped Discord user authorization.

``channel_allowed_users`` in ``config.yaml`` maps Discord channel snowflake IDs
to lists of user snowflake IDs. A listed user is authorized only in that channel
or a thread whose adapter-resolved parent ID is that channel. Global Discord
authorization continues to use ``allow_from`` / ``DISCORD_ALLOWED_USERS``.

Authorization is a **union of grants** across pairing, channel-scoped entries,
global user/role allowlists, allow-all flags, and ``DISCORD_ALLOWED_CHANNELS``
(when no user/role allowlists are configured). The adapter and gateway layers
evaluate different subsets of that union; outcomes match because any single
grant is sufficient. When no scoped mapping is configured, behavior is unchanged.

Enforcement matches **immutable numeric snowflake IDs only** — never channel
names, ``#name`` forms, or display names. Slash authorization passes mixed
name/ID key sets into ``_is_allowed_user``; the scoped grant filters to
digit-only IDs before matching so a renamed channel cannot collide with another
channel's snowflake.

Scoped lists treat ``"*"`` as a **literal** user-ID string, not an allow-all
wildcard (unlike global ``allow_from`` / ``DISCORD_ALLOWED_USERS``).

Scoped entries require bare numeric snowflakes. Unlike global ``allow_from``,
``<@id>`` / ``user:`` prefixes are **not** stripped — malformed paste forms
fail closed rather than accidentally matching.
"""

from __future__ import annotations

import json
from collections.abc import Iterable


def _clean_ids(values: object) -> frozenset[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(value).strip() for value in values if str(value).strip())


def channel_user_allowlists(raw: object = None) -> dict[str, frozenset[str]]:
    """Parse the scoped allowlist, returning an empty policy on malformed input."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        decoded = raw
    elif isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(decoded, dict):
            return {}
    else:
        return {}

    result: dict[str, frozenset[str]] = {}
    for channel_id, users in decoded.items():
        clean_channel = str(channel_id).strip()
        clean_users = _clean_ids(users)
        if clean_channel and clean_users:
            result[clean_channel] = clean_users
    return result


def is_channel_scoped_user_allowed(
    user_id: str,
    channel_ids: Iterable[str] | None,
    raw: object = None,
) -> bool:
    """Return True only when user and current/parent channel match one entry."""
    clean_user = str(user_id or "").strip()
    if not clean_user or channel_ids is None:
        return False
    policy = channel_user_allowlists(raw)
    if not policy:
        return False
    for channel_id in channel_ids:
        allowed_users = policy.get(str(channel_id).strip())
        if allowed_users and clean_user in allowed_users:
            return True
    return False
