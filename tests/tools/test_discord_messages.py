"""Tests for tools.discord_api.messages request builders (feature M2)."""

import pytest

from tools.discord_api.messages import (
    BULK_DELETE_MAX,
    BULK_DELETE_MIN,
    MAX_CONTENT_LENGTH,
    MessageError,
    delete_message_request,
    delete_messages_bulk_request,
    edit_message_request,
)

CHANNEL = "123456789012345678"
MESSAGE = "876543210987654321"


def test_message_error_is_value_error():
    assert issubclass(MessageError, ValueError)


def test_edit_message_request_shape_and_content():
    req = edit_message_request(CHANNEL, MESSAGE, content="hello world")
    assert req["method"] == "PATCH"
    assert req["path"] == f"/channels/{CHANNEL}/messages/{MESSAGE}"
    assert req["payload"] == {"content": "hello world"}
    assert req["query"] == {}


def test_edit_message_request_with_embeds_and_flags():
    embeds = [{"title": "Hi", "description": "there"}]
    req = edit_message_request(CHANNEL, MESSAGE, embeds=embeds, flags=1 << 6)
    assert req["payload"] == {"embeds": embeds, "flags": 1 << 6}


def test_edit_message_request_no_fields_returns_empty_payload():
    req = edit_message_request(CHANNEL, MESSAGE)
    assert req["payload"] == {}
    assert req["path"] == f"/channels/{CHANNEL}/messages/{MESSAGE}"


def test_delete_message_request_shape():
    req = delete_message_request(CHANNEL, MESSAGE)
    assert req["method"] == "DELETE"
    assert req["path"] == f"/channels/{CHANNEL}/messages/{MESSAGE}"
    assert req["payload"] == {}
    assert req["query"] == {}


def test_delete_messages_bulk_request_payload():
    ids = ["100000000000000001", "100000000000000002", "100000000000000003"]
    req = delete_messages_bulk_request(CHANNEL, ids)
    assert req["method"] == "POST"
    assert req["path"] == f"/channels/{CHANNEL}/messages/bulk-delete"
    assert req["payload"] == {"messages": ids}
    assert req["query"] == {}


@pytest.mark.parametrize("count", [BULK_DELETE_MIN, 50, BULK_DELETE_MAX])
def test_delete_messages_bulk_request_bounds_ok(count):
    ids = [str(i) for i in range(1, count + 1)]
    req = delete_messages_bulk_request(CHANNEL, ids)
    assert len(req["payload"]["messages"]) == count


@pytest.mark.parametrize("count", [0, 1, BULK_DELETE_MAX + 1])
def test_delete_messages_bulk_request_bounds_rejected(count):
    ids = [str(i) for i in range(1, count + 1)]
    with pytest.raises(MessageError):
        delete_messages_bulk_request(CHANNEL, ids)


@pytest.mark.parametrize(
    "bad",
    ["", "12a34", "12-34", "   ", "123 456", "١٢٣"],
)
def test_invalid_snowflake_rejected(bad):
    with pytest.raises(MessageError):
        edit_message_request(bad, MESSAGE)
    with pytest.raises(MessageError):
        edit_message_request(CHANNEL, bad)
    with pytest.raises(MessageError):
        delete_message_request(bad, MESSAGE)
    with pytest.raises(MessageError):
        delete_message_request(CHANNEL, bad)
    with pytest.raises(MessageError):
        delete_messages_bulk_request(bad, ["1", "2"])
    with pytest.raises(MessageError):
        delete_messages_bulk_request(CHANNEL, ["1", bad, "2"])


def test_non_string_snowflake_rejected():
    with pytest.raises(MessageError):
        edit_message_request(123, MESSAGE)
    with pytest.raises(MessageError):
        edit_message_request(CHANNEL, 456)
    with pytest.raises(MessageError):
        delete_messages_bulk_request(CHANNEL, [1, 2, 3])


def test_message_ids_must_be_a_list():
    with pytest.raises(MessageError):
        delete_messages_bulk_request(CHANNEL, "12")


def test_content_too_long_rejected():
    with pytest.raises(MessageError):
        edit_message_request(CHANNEL, MESSAGE, content="a" * (MAX_CONTENT_LENGTH + 1))


def test_content_max_length_accepted():
    req = edit_message_request(CHANNEL, MESSAGE, content="a" * MAX_CONTENT_LENGTH)
    assert len(req["payload"]["content"]) == MAX_CONTENT_LENGTH
