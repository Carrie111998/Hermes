"""Tests for plugin-owned Discord reaction REST request builders."""

import pytest

from plugins.platforms.discord.reaction_requests import (
    MAX_REACTION_PAGE,
    ReactionError,
    add_reaction_request,
    encode_emoji_path,
    list_reactions_request,
    remove_own_reaction_request,
    validate_emoji,
)


def test_unicode_and_keycap_emoji_paths_are_encoded():
    assert encode_emoji_path("👍") == "%F0%9F%91%8D"
    assert encode_emoji_path("1️⃣") == "1%EF%B8%8F%E2%83%A3"


def test_custom_emoji_path_is_encoded():
    assert encode_emoji_path("hermes:123456789012345678") == "hermes%3A123456789012345678"


@pytest.mark.parametrize("emoji", [" hello", "hello", "<@123>", "x@everyone", "bad:12"])
def test_invalid_emoji_is_rejected(emoji):
    with pytest.raises(ReactionError):
        validate_emoji(emoji)


def test_add_and_remove_own_request_shapes():
    assert add_reaction_request("111", "222", "👍") == {
        "method": "PUT", "path": "/channels/111/messages/222/reactions/%F0%9F%91%8D/@me", "payload": None, "query": {}
    }
    assert remove_own_reaction_request("111", "222", "👍")["method"] == "DELETE"


def test_list_limit_is_clamped_and_invalid_values_raise_reaction_error():
    assert list_reactions_request("111", "222", "👍", limit=999)["query"] == {"limit": str(MAX_REACTION_PAGE)}
    with pytest.raises(ReactionError):
        list_reactions_request("111", "222", "👍", limit="not-a-number")
