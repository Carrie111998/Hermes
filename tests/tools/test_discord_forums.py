"""Tests for tools.discord_api.forums — forum starter/tag REST request builders."""

import pytest

from tools.discord_api.forums import (
    FORUM_CHANNEL_TYPE,
    ForumError,
    create_forum_post_request,
    list_forum_tags_request,
    set_forum_post_tags_request,
)


# ---------------------------------------------------------------------------
# create_forum_post_request — payload shape
# ---------------------------------------------------------------------------


def test_create_post_full_payload():
    req = create_forum_post_request(
        "123456789012345678",
        name="Need help with setup",
        message_content="Details here",
        applied_tags=["111111111111111111", "222222222222222222"],
    )
    assert req["method"] == "POST"
    assert req["path"] == "/channels/123456789012345678/threads"
    assert req["payload"] == {
        "name": "Need help with setup",
        "type": FORUM_CHANNEL_TYPE,
        "message": {"content": "Details here"},
        "applied_tags": ["111111111111111111", "222222222222222222"],
    }


def test_create_post_minimal_payload():
    req = create_forum_post_request("123", name="Hello")
    assert req["payload"] == {"name": "Hello", "type": FORUM_CHANNEL_TYPE}
    # optional keys are omitted, not present as None
    assert "message" not in req["payload"]
    assert "applied_tags" not in req["payload"]


def test_create_post_int_channel_and_tag_ids_normalized_to_str():
    req = create_forum_post_request(
        123456789012345678,
        name="Post",
        message_content="body",
        applied_tags=[111, 222],
    )
    assert req["path"] == "/channels/123456789012345678/threads"
    assert req["payload"]["applied_tags"] == ["111", "222"]


# ---------------------------------------------------------------------------
# create_forum_post_request — name bounds
# ---------------------------------------------------------------------------


def test_create_post_name_bounds_ok():
    one_char = create_forum_post_request("1", name="a")
    assert one_char["payload"]["name"] == "a"
    hundred_char = create_forum_post_request("1", name="x" * 100)
    assert hundred_char["payload"]["name"] == "x" * 100


def test_create_post_name_too_short():
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="")


def test_create_post_name_too_long():
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="x" * 101)


def test_create_post_name_not_str():
    with pytest.raises(ForumError):
        create_forum_post_request("1", name=123)


# ---------------------------------------------------------------------------
# tag count bounds
# ---------------------------------------------------------------------------


def test_create_post_max_five_tags_ok():
    tags = [str(i) for i in range(5)]
    req = create_forum_post_request("1", name="Post", applied_tags=tags)
    assert req["payload"]["applied_tags"] == tags


def test_create_post_six_tags_raises():
    tags = [str(i) for i in range(6)]
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="Post", applied_tags=tags)


def test_set_tags_max_five_ok_and_empty_ok():
    tags = [str(i) for i in range(5)]
    req = set_forum_post_tags_request("42", tags)
    assert req["payload"] == {"applied_tags": tags}
    empty = set_forum_post_tags_request("42", [])
    assert empty["payload"] == {"applied_tags": []}


def test_set_tags_six_raises():
    tags = [str(i) for i in range(6)]
    with pytest.raises(ForumError):
        set_forum_post_tags_request("42", tags)


def test_applied_tags_not_a_list_raises():
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="Post", applied_tags="111")
    with pytest.raises(ForumError):
        set_forum_post_tags_request("42", "111")


# ---------------------------------------------------------------------------
# snowflake validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "abc", "12a34", "-5", "1.5", "  ", None, True, 3.14, ["1"], {}],
)
def test_create_post_invalid_channel_id_raises(bad):
    with pytest.raises(ForumError):
        create_forum_post_request(bad, name="Post")


@pytest.mark.parametrize("bad", ["", "abc", "12a34", "-5", None, True, 3.14])
def test_create_post_invalid_tag_id_raises(bad):
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="Post", applied_tags=[bad])
    with pytest.raises(ForumError):
        create_forum_post_request("1", name="Post", applied_tags=["1", bad])


@pytest.mark.parametrize("bad", ["", "abc", "12a34", "-5", None, True, 3.14])
def test_set_tags_invalid_tag_id_raises(bad):
    with pytest.raises(ForumError):
        set_forum_post_tags_request("42", [bad])


def test_invalid_thread_id_raises():
    with pytest.raises(ForumError):
        set_forum_post_tags_request("nope", ["1"])


# ---------------------------------------------------------------------------
# list_forum_tags_request
# ---------------------------------------------------------------------------


def test_list_forum_tags_request():
    req = list_forum_tags_request("987654321")
    assert req["method"] == "GET"
    assert req["path"] == "/channels/987654321/tags"
    assert req["payload"] is None


def test_list_forum_tags_request_invalid_channel_raises():
    with pytest.raises(ForumError):
        list_forum_tags_request("not-a-snowflake")


# ---------------------------------------------------------------------------
# set_forum_post_tags_request — payload shape
# ---------------------------------------------------------------------------


def test_set_tags_payload():
    req = set_forum_post_tags_request("777", ["111", "222", "333"])
    assert req["method"] == "PATCH"
    assert req["path"] == "/channels/777"
    assert req["payload"] == {"applied_tags": ["111", "222", "333"]}


def test_set_tags_int_ids_normalized_to_str():
    req = set_forum_post_tags_request(777, [111, 222])
    assert req["payload"] == {"applied_tags": ["111", "222"]}


def test_set_tags_missing_raises():
    with pytest.raises(ForumError):
        set_forum_post_tags_request("777", None)


# ---------------------------------------------------------------------------
# error type
# ---------------------------------------------------------------------------


def test_forum_error_is_value_error():
    assert issubclass(ForumError, ValueError)
    with pytest.raises(ValueError):
        create_forum_post_request("1", name="x" * 200)
