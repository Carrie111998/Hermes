"""Tests for tools.discord_api.channels request builders (feature A1).

Covers request descriptor shapes for create/edit/delete, name bounds,
topic bound, rate_limit_per_user bounds, snowflake validation, and
only-provided-fields editing semantics.
"""

import pytest

from tools.discord_api.channels import (
    ChannelError,
    create_channel_request,
    delete_channel_request,
    edit_channel_request,
)

GUILD = "123456789012345678"
CHANNEL = "987654321098765432"
PARENT = "112233445566778899"

MAX_SNOWFLAKE = 2**63 - 1


# ---------------------------------------------------------------------------
# create_channel_request
# ---------------------------------------------------------------------------

def test_create_channel_request_shape():
    req = create_channel_request(GUILD, name="general")
    assert req == {
        "method": "POST",
        "path": f"/guilds/{GUILD}/channels",
        "json": {
            "name": "general",
            "type": 0,
            "nsfw": False,
            "rate_limit_per_user": 0,
        },
    }


def test_create_channel_full_payload():
    req = create_channel_request(
        GUILD,
        name="mod-archive",
        type=4,
        topic="Archived mod threads",
        nsfw=True,
        rate_limit_per_user=30,
        parent_id=PARENT,
    )
    assert req["method"] == "POST"
    assert req["path"] == f"/guilds/{GUILD}/channels"
    assert req["json"] == {
        "name": "mod-archive",
        "type": 4,
        "topic": "Archived mod threads",
        "nsfw": True,
        "rate_limit_per_user": 30,
        "parent_id": PARENT,
    }


def test_create_category_via_type_4():
    req = create_channel_request(GUILD, name="Games", type=4)
    assert req["json"]["type"] == 4


def test_create_name_is_trimmed():
    req = create_channel_request(GUILD, name="  general  ")
    assert req["json"]["name"] == "general"


def test_create_name_empty_raises():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="   ")


def test_create_name_too_long_raises():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="a" * 101)


def test_create_name_100_chars_ok():
    req = create_channel_request(GUILD, name="a" * 100)
    assert req["json"]["name"] == "a" * 100


def test_create_name_non_string_raises():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name=123)


def test_create_topic_too_long_raises():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", topic="t" * 1025)


def test_create_topic_1024_chars_ok():
    req = create_channel_request(GUILD, name="x", topic="t" * 1024)
    assert req["json"]["topic"] == "t" * 1024


def test_create_topic_omitted_when_none():
    req = create_channel_request(GUILD, name="x")
    assert "topic" not in req["json"]


def test_create_rate_limit_bounds():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", rate_limit_per_user=-1)
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", rate_limit_per_user=21601)
    req = create_channel_request(GUILD, name="x", rate_limit_per_user=21600)
    assert req["json"]["rate_limit_per_user"] == 21600
    req = create_channel_request(GUILD, name="x", rate_limit_per_user=0)
    assert req["json"]["rate_limit_per_user"] == 0


def test_create_rate_limit_non_int_raises():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", rate_limit_per_user="10")


def test_create_type_must_be_int():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", type="text")
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", type=True)


def test_create_nsfw_must_be_bool():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", nsfw="yes")


def test_create_guild_snowflake_validation():
    for bad in ("", "abc", "12a4", "-123", "1.5", None, MAX_SNOWFLAKE + 1):
        with pytest.raises(ChannelError):
            create_channel_request(bad, name="x")


def test_create_parent_id_snowflake_validation():
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", parent_id="not-a-snowflake")
    with pytest.raises(ChannelError):
        create_channel_request(GUILD, name="x", parent_id=-5)


def test_create_accepts_int_snowflakes():
    req = create_channel_request(123456789, name="x", parent_id=987654321)
    assert req["path"] == "/guilds/123456789/channels"
    assert req["json"]["parent_id"] == "987654321"


# ---------------------------------------------------------------------------
# edit_channel_request
# ---------------------------------------------------------------------------

def test_edit_channel_request_shape():
    req = edit_channel_request(CHANNEL, name="renamed")
    assert req["method"] == "PATCH"
    assert req["path"] == f"/channels/{CHANNEL}"
    assert req["json"] == {"name": "renamed"}


def test_edit_only_provided_fields():
    req = edit_channel_request(CHANNEL, topic="new topic")
    assert req["json"] == {"topic": "new topic"}
    req = edit_channel_request(CHANNEL, nsfw=True)
    assert req["json"] == {"nsfw": True}
    req = edit_channel_request(CHANNEL, parent_id=PARENT)
    assert req["json"] == {"parent_id": PARENT}


def test_edit_multiple_fields():
    req = edit_channel_request(
        CHANNEL, name="x", topic="t", nsfw=False, parent_id=PARENT
    )
    assert req["json"] == {
        "name": "x",
        "topic": "t",
        "nsfw": False,
        "parent_id": PARENT,
    }


def test_edit_no_fields_raises():
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL)


def test_edit_name_validated():
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL, name="   ")
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL, name="a" * 101)


def test_edit_name_is_trimmed():
    req = edit_channel_request(CHANNEL, name="  padded  ")
    assert req["json"]["name"] == "padded"


def test_edit_topic_validated():
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL, topic="t" * 1025)


def test_edit_channel_snowflake_validation():
    with pytest.raises(ChannelError):
        edit_channel_request("bad-id", name="x")


def test_edit_nsfw_must_be_bool():
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL, nsfw=1)


def test_edit_parent_snowflake_validation():
    with pytest.raises(ChannelError):
        edit_channel_request(CHANNEL, parent_id="nope")


# ---------------------------------------------------------------------------
# delete_channel_request
# ---------------------------------------------------------------------------

def test_delete_channel_request_shape():
    req = delete_channel_request(CHANNEL)
    assert req == {"method": "DELETE", "path": f"/channels/{CHANNEL}", "json": None}


def test_delete_channel_snowflake_validation():
    with pytest.raises(ChannelError):
        delete_channel_request("nope")
    with pytest.raises(ChannelError):
        delete_channel_request(MAX_SNOWFLAKE + 1)


def test_delete_accepts_int_snowflake():
    req = delete_channel_request(42)
    assert req["path"] == "/channels/42"


# ---------------------------------------------------------------------------
# error semantics
# ---------------------------------------------------------------------------

def test_channel_error_is_value_error():
    assert issubclass(ChannelError, ValueError)
    with pytest.raises(ValueError):
        delete_channel_request("bad")
