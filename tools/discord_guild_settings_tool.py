"""Request-owned Discord guild-settings consumer.

The model may choose *which approved scalar fields* to change, but never the
Discord guild, requester, profile credential, or transport. Those identities
come from the task-local gateway context and the existing Discord REST adapter.
"""

from __future__ import annotations

import json
from typing import Any

from gateway.session_context import get_session_env
from tools import discord_tool as _discord
from tools.discord_api.guild_settings import GuildSettingsError, edit_guild_request
from tools.registry import registry, tool_error

_ACTION_NAME = "edit_current_guild_settings"

_SETTINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Approved scalar settings for the active Discord request guild. "
        "The target guild is request-owned and cannot be supplied by the model."
    ),
    "properties": {
        "name": {"type": "string", "minLength": 2, "maxLength": 100},
        "description": {
            "anyOf": [
                {"type": "string", "maxLength": 1024},
                {"type": "null"},
            ]
        },
        "verification_level": {
            "anyOf": [
                {"type": "integer", "enum": [0, 1, 2, 3, 4]},
                {"type": "null"},
            ]
        },
        "default_message_notifications": {
            "anyOf": [
                {"type": "integer", "enum": [0, 1]},
                {"type": "null"},
            ]
        },
        "explicit_content_filter": {
            "anyOf": [
                {"type": "integer", "enum": [0, 1, 2]},
                {"type": "null"},
            ]
        },
        "premium_progress_bar_enabled": {"type": "boolean"},
        "afk_channel_id": {
            "anyOf": [
                {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                {"type": "null"},
            ]
        },
        "system_channel_id": {
            "anyOf": [
                {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                {"type": "null"},
            ]
        },
        "rules_channel_id": {
            "anyOf": [
                {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                {"type": "null"},
            ]
        },
        "public_updates_channel_id": {
            "anyOf": [
                {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                {"type": "null"},
            ]
        },
        "safety_alerts_channel_id": {
            "anyOf": [
                {"type": "string", "pattern": "^[1-9][0-9]{0,19}$"},
                {"type": "null"},
            ]
        },
        "afk_timeout": {
            "type": "integer",
            "enum": [60, 300, 900, 1800, 3600],
        },
    },
    "additionalProperties": False,
}

SCHEMA = {
    "name": "discord_guild_settings",
    "description": (
        "Edit approved scalar settings on the Discord guild that owns the "
        "active authenticated request. The target cannot be redirected to a "
        "model-supplied guild ID. Requires the bot's MANAGE_GUILD permission."
    ),
    "parameters": {
        "type": "object",
        "properties": {"settings": _SETTINGS_SCHEMA},
        "required": ["settings"],
        "additionalProperties": False,
    },
}


def _action_enabled() -> bool:
    allowlist = _discord._load_allowed_actions_config()
    return allowlist is None or _ACTION_NAME in allowlist


def check_discord_guild_settings_requirements() -> bool:
    """Require the active profile token and the shared admin-action gate."""
    return bool(_discord._get_bot_token()) and _action_enabled()


def edit_current_guild_settings(settings: Any = None) -> str:
    """Validate and PATCH the exact guild that owns the active Discord turn."""
    platform = get_session_env("HERMES_SESSION_PLATFORM").strip().lower()
    requester_id = get_session_env("HERMES_SESSION_USER_ID").strip()
    guild_id = get_session_env("HERMES_SESSION_SCOPE_ID").strip()

    if platform != "discord":
        return tool_error(
            "discord_guild_settings requires an active Discord request context."
        )
    if not requester_id:
        return tool_error(
            "discord_guild_settings requires an authenticated Discord requester."
        )
    if not guild_id:
        return tool_error(
            "discord_guild_settings requires an active Discord guild context; "
            "it is unavailable in DMs and unowned cross-platform sessions."
        )
    if not _action_enabled():
        return tool_error(
            "Action 'edit_current_guild_settings' is disabled by config "
            "(discord.server_actions)."
        )
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        return tool_error("'settings' must be a JSON object.")

    token = _discord._get_bot_token()
    if not token:
        return tool_error("DISCORD_BOT_TOKEN not configured for the active profile.")

    try:
        request = edit_guild_request(guild_id, **settings)
        _discord._discord_request(
            request["method"],
            request["path"],
            token,
            body=request["json"],
        )
    except GuildSettingsError as exc:
        return tool_error(str(exc))
    except _discord.DiscordAPIError as exc:
        if exc.status == 403:
            return tool_error(
                "Discord API 403 (forbidden) on 'edit_current_guild_settings'. "
                "Bot lacks MANAGE_GUILD in the active Discord server. "
                f"(Raw: {exc.body})"
            )
        return tool_error(str(exc))
    except Exception as exc:
        return tool_error(f"Unexpected Discord guild-settings error: {exc}")

    canonical_guild_id = request["path"].rsplit("/", 1)[-1]
    return json.dumps(
        {
            "success": True,
            "guild_id": canonical_guild_id,
            "updated_settings": request["json"],
        }
    )


def _handler(args: dict[str, Any], **_kwargs: Any) -> str:
    return edit_current_guild_settings(args.get("settings"))


registry.register(
    name="discord_guild_settings",
    toolset="discord_admin",
    schema=SCHEMA,
    handler=_handler,
    check_fn=check_discord_guild_settings_requirements,
    requires_env=["DISCORD_BOT_TOKEN"],
)
