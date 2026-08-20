"""Cross-layer contract for the request-owned guild-settings consumer."""

import json
from unittest.mock import Mock

import pytest

from gateway.session_context import clear_session_vars, reset_session_vars, set_session_vars
from tools import discord_guild_settings_tool as consumer

CURRENT_GUILD = "123456789012345678"
OTHER_GUILD = "999999999999999999"
CHANNEL_ID = "987654321098765432"


@pytest.fixture(autouse=True)
def _isolated_consumer(monkeypatch):
    reset_session_vars()
    monkeypatch.setattr(consumer._discord, "_get_bot_token", lambda: "active-profile-token")
    monkeypatch.setattr(consumer._discord, "_load_allowed_actions_config", lambda: None)
    yield
    reset_session_vars()


def _bind_discord_request(*, guild_id: str = CURRENT_GUILD, user_id: str = "42"):
    return set_session_vars(
        platform="discord",
        user_id=user_id,
        scope_id=guild_id,
        profile="worker",
        session_key=f"agent:worker:discord:channel:{guild_id}:123",
    )


def _error(result: str) -> str:
    return str(json.loads(result)["error"])


def test_consumer_uses_request_owned_guild_and_active_profile_token(monkeypatch):
    request = Mock(return_value={"id": CURRENT_GUILD})
    monkeypatch.setattr(consumer._discord, "_discord_request", request)
    tokens = _bind_discord_request(guild_id=f"000{CURRENT_GUILD}")
    try:
        result = json.loads(
            consumer.edit_current_guild_settings(
                {
                    "name": "Hermes HQ",
                    "system_channel_id": f"000{CHANNEL_ID}",
                    "afk_timeout": 300,
                }
            )
        )
    finally:
        clear_session_vars(tokens)

    request.assert_called_once_with(
        "PATCH",
        f"/guilds/{CURRENT_GUILD}",
        "active-profile-token",
        body={
            "name": "Hermes HQ",
            "system_channel_id": CHANNEL_ID,
            "afk_timeout": 300,
        },
    )
    assert result == {
        "success": True,
        "guild_id": CURRENT_GUILD,
        "updated_settings": {
            "name": "Hermes HQ",
            "system_channel_id": CHANNEL_ID,
            "afk_timeout": 300,
        },
    }
    assert OTHER_GUILD not in json.dumps(result)


def test_consumer_preserves_explicit_false_zero_and_null(monkeypatch):
    request = Mock(return_value={"id": CURRENT_GUILD})
    monkeypatch.setattr(consumer._discord, "_discord_request", request)
    tokens = _bind_discord_request()
    try:
        result = json.loads(
            consumer.edit_current_guild_settings(
                {
                    "premium_progress_bar_enabled": False,
                    "default_message_notifications": 0,
                    "description": None,
                    "afk_channel_id": None,
                }
            )
        )
    finally:
        clear_session_vars(tokens)

    expected = {
        "premium_progress_bar_enabled": False,
        "default_message_notifications": 0,
        "description": None,
        "afk_channel_id": None,
    }
    request.assert_called_once_with(
        "PATCH",
        f"/guilds/{CURRENT_GUILD}",
        "active-profile-token",
        body=expected,
    )
    assert result["updated_settings"] == expected


@pytest.mark.parametrize(
    ("platform", "user_id", "guild_id", "message"),
    [
        ("slack", "42", CURRENT_GUILD, "active Discord request context"),
        ("discord", "", CURRENT_GUILD, "authenticated Discord requester"),
        ("discord", "42", "", "active Discord guild context"),
    ],
)
def test_consumer_fails_closed_without_complete_request_owner(
    monkeypatch,
    platform,
    user_id,
    guild_id,
    message,
):
    request = Mock()
    monkeypatch.setattr(consumer._discord, "_discord_request", request)
    tokens = set_session_vars(platform=platform, user_id=user_id, scope_id=guild_id)
    try:
        result = consumer.edit_current_guild_settings({"name": "Hermes HQ"})
    finally:
        clear_session_vars(tokens)

    assert message in _error(result)
    request.assert_not_called()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({}, "no guild settings provided"),
        ({"nsfw_level": 1}, "unsupported guild setting"),
        ({"afk_timeout": 61}, "must be one of"),
        ([], "must be a JSON object"),
    ],
)
def test_consumer_rejects_invalid_or_empty_patch_before_transport(
    monkeypatch,
    settings,
    message,
):
    request = Mock()
    monkeypatch.setattr(consumer._discord, "_discord_request", request)
    tokens = _bind_discord_request()
    try:
        result = consumer.edit_current_guild_settings(settings)
    finally:
        clear_session_vars(tokens)

    assert message in _error(result)
    request.assert_not_called()


def test_consumer_respects_shared_server_action_allowlist(monkeypatch):
    request = Mock()
    monkeypatch.setattr(consumer._discord, "_discord_request", request)
    monkeypatch.setattr(consumer._discord, "_load_allowed_actions_config", lambda: ["list_roles"])
    tokens = _bind_discord_request()
    try:
        result = consumer.edit_current_guild_settings({"name": "Hermes HQ"})
    finally:
        clear_session_vars(tokens)

    assert "disabled by config" in _error(result)
    assert consumer.check_discord_guild_settings_requirements() is False
    request.assert_not_called()


def test_schema_exposes_only_owned_settings_and_no_target_id():
    parameters = consumer.SCHEMA["parameters"]
    assert set(parameters["properties"]) == {"settings"}
    assert parameters["additionalProperties"] is False
    settings = parameters["properties"]["settings"]
    assert settings["additionalProperties"] is False
    assert "nsfw_level" not in settings["properties"]
    assert settings["properties"]["afk_timeout"]["enum"] == [60, 300, 900, 1800, 3600]


def test_sequential_request_owners_do_not_share_guild_authority(monkeypatch):
    request = Mock(return_value={})
    monkeypatch.setattr(consumer._discord, "_discord_request", request)

    first = _bind_discord_request(guild_id=CURRENT_GUILD)
    try:
        consumer.edit_current_guild_settings({"name": "First Guild"})
    finally:
        clear_session_vars(first)

    second_guild = "222222222222222222"
    second = _bind_discord_request(guild_id=second_guild, user_id="84")
    try:
        consumer.edit_current_guild_settings({"name": "Second Guild"})
    finally:
        clear_session_vars(second)

    assert request.call_args_list[0].args[:3] == (
        "PATCH",
        f"/guilds/{CURRENT_GUILD}",
        "active-profile-token",
    )
    assert request.call_args_list[1].args[:3] == (
        "PATCH",
        f"/guilds/{second_guild}",
        "active-profile-token",
    )
