"""Tests for the Discord guild-settings request contract."""

import pytest

from tools.discord_api.guild_settings import GuildSettingsError, edit_guild_request

GUILD_ID = "123456789012345678"
CHANNEL_ID = "987654321098765432"


def test_allowed_scalar_edits_full_payload():
    request = edit_guild_request(
        GUILD_ID,
        name="Hermes HQ",
        description="A fine place",
        verification_level=4,
        default_message_notifications=1,
        explicit_content_filter=2,
        nsfw_level=3,
        premium_progress_bar_enabled=True,
        system_channel_id=CHANNEL_ID,
        rules_channel_id=CHANNEL_ID,
        public_updates_channel_id=CHANNEL_ID,
        afk_timeout=3600,
    )

    assert request == {
        "method": "PATCH",
        "path": f"/guilds/{GUILD_ID}",
        "json": {
            "name": "Hermes HQ",
            "description": "A fine place",
            "verification_level": 4,
            "default_message_notifications": 1,
            "explicit_content_filter": 2,
            "nsfw_level": 3,
            "premium_progress_bar_enabled": True,
            "system_channel_id": CHANNEL_ID,
            "rules_channel_id": CHANNEL_ID,
            "public_updates_channel_id": CHANNEL_ID,
            "afk_timeout": 3600,
        },
    }


def test_minimum_allowed_values_and_nullable_fields():
    request = edit_guild_request(
        GUILD_ID,
        verification_level=0,
        default_message_notifications=0,
        explicit_content_filter=0,
        nsfw_level=0,
        premium_progress_bar_enabled=False,
        afk_timeout=60,
        description=None,
        system_channel_id=None,
    )

    assert request["json"] == {
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "nsfw_level": 0,
        "premium_progress_bar_enabled": False,
        "afk_timeout": 60,
        "description": None,
        "system_channel_id": None,
    }


@pytest.mark.parametrize(
    "bad_key",
    ["widget_enabled", "system_channel_flags", "bogus_field"],
)
def test_disallowed_key_rejected(bad_key):
    with pytest.raises(GuildSettingsError, match="unsupported guild setting"):
        edit_guild_request(GUILD_ID, **{bad_key: True})


def test_name_max_length_ok():
    assert edit_guild_request(GUILD_ID, name="x" * 100)["json"]["name"] == "x" * 100


def test_name_too_long_rejected():
    with pytest.raises(GuildSettingsError, match="exceeds 100"):
        edit_guild_request(GUILD_ID, name="x" * 101)


def test_name_must_be_string():
    with pytest.raises(GuildSettingsError, match="must be a string"):
        edit_guild_request(GUILD_ID, name=123)


def test_description_max_ok():
    assert len(edit_guild_request(GUILD_ID, description="x" * 1024)["json"]["description"]) == 1024


def test_description_too_long_rejected():
    with pytest.raises(GuildSettingsError, match="exceeds 1024"):
        edit_guild_request(GUILD_ID, description="x" * 1025)


@pytest.mark.parametrize("level", [-1, 5, 100])
def test_verification_level_out_of_range(level):
    with pytest.raises(GuildSettingsError, match="between 0 and 4"):
        edit_guild_request(GUILD_ID, verification_level=level)


@pytest.mark.parametrize("bad", [True, "3", 3.5])
def test_verification_level_wrong_type(bad):
    with pytest.raises(GuildSettingsError, match="must be an integer"):
        edit_guild_request(GUILD_ID, verification_level=bad)


@pytest.mark.parametrize(
    "field",
    ["system_channel_id", "rules_channel_id", "public_updates_channel_id"],
)
@pytest.mark.parametrize(
    "bad",
    ["not-a-snowflake", "123abc", -5, 0, 2**64, 1.5, True],
)
def test_channel_id_invalid_rejected(field, bad):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(GUILD_ID, **{field: bad})


def test_channel_id_none_allowed():
    assert edit_guild_request(GUILD_ID, system_channel_id=None)["json"] == {
        "system_channel_id": None
    }


def test_snowflakes_are_canonical_decimal_strings():
    request = edit_guild_request(
        123456789012345678,
        rules_channel_id=987654321098765432,
        system_channel_id="000987654321098765432",
    )

    assert request["path"] == "/guilds/123456789012345678"
    assert request["json"]["rules_channel_id"] == "987654321098765432"
    assert request["json"]["system_channel_id"] == "987654321098765432"


@pytest.mark.parametrize("guild_id", ["guild-abc", "", "0000", 0, -1, 2**64, True])
def test_invalid_guild_id_rejected(guild_id):
    with pytest.raises(GuildSettingsError):
        edit_guild_request(guild_id, name="Hermes")


@pytest.mark.parametrize("timeout", [59, 3601, 0, -1])
def test_afk_timeout_out_of_range(timeout):
    with pytest.raises(GuildSettingsError, match="between 60 and 3600"):
        edit_guild_request(GUILD_ID, afk_timeout=timeout)


def test_only_provided_fields_in_payload():
    assert edit_guild_request(GUILD_ID, name="Renamed")["json"] == {
        "name": "Renamed"
    }


def test_empty_patch_rejected():
    with pytest.raises(GuildSettingsError, match="no guild settings provided"):
        edit_guild_request(GUILD_ID)
