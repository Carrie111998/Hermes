"""Tests for the tools.discord_api.threads request builders."""

import pytest

from tools.discord_api.threads import (
    NAME_MAX_LENGTH,
    NAME_MIN_LENGTH,
    ThreadError,
    archive_thread_request,
    join_thread_request,
    list_active_threads_request,
    rename_thread_request,
    start_thread_request,
)

CHANNEL = "123456789012345678"
MESSAGE = "234567890123456789"
THREAD = "345678901234567890"
MAX_SNOWFLAKE = (1 << 63) - 1


# ---------------------------------------------------------------- shapes


def test_start_thread_public_shape():
    req = start_thread_request(CHANNEL, name="general-news", type=11)
    assert req == {
        "method": "POST",
        "path": f"/channels/{CHANNEL}/threads",
        "payload": {"name": "general-news", "type": 11},
        "query": {},
    }


def test_start_thread_from_message_shape():
    req = start_thread_request(CHANNEL, name="from-msg", message_id=MESSAGE, type=12)
    assert req == {
        "method": "POST",
        "path": f"/channels/{CHANNEL}/messages/{MESSAGE}/threads",
        "payload": {"name": "from-msg", "type": 12},
        "query": {},
    }


def test_start_thread_without_type_omits_key():
    req = start_thread_request(CHANNEL, name="plain")
    assert req == {
        "method": "POST",
        "path": f"/channels/{CHANNEL}/threads",
        "payload": {"name": "plain"},
        "query": {},
    }


def test_rename_thread_shape():
    req = rename_thread_request(THREAD, name="new name")
    assert req == {
        "method": "PATCH",
        "path": f"/channels/{THREAD}",
        "payload": {"name": "new name"},
        "query": {},
    }


def test_archive_thread_shape():
    req = archive_thread_request(THREAD)
    assert req == {
        "method": "PATCH",
        "path": f"/channels/{THREAD}",
        "payload": {"archived": True, "locked": False},
        "query": {},
    }


def test_list_active_threads_shape():
    req = list_active_threads_request(CHANNEL)
    assert req == {
        "method": "GET",
        "path": f"/channels/{CHANNEL}/threads/active",
        "payload": None,
        "query": {},
    }


def test_join_thread_shape():
    req = join_thread_request(THREAD)
    assert req == {
        "method": "PUT",
        "path": f"/channels/{THREAD}/thread-members/@me",
        "payload": None,
        "query": {},
    }


def test_descriptor_has_expected_keys():
    for req in (
        start_thread_request(CHANNEL, name="x"),
        rename_thread_request(THREAD, name="x"),
        archive_thread_request(THREAD),
        list_active_threads_request(CHANNEL),
        join_thread_request(THREAD),
    ):
        assert set(req) == {"method", "path", "payload", "query"}


# ------------------------------------------------------- thread types


@pytest.mark.parametrize("thread_type", [10, 11, 12])
def test_start_thread_accepts_valid_types(thread_type):
    req = start_thread_request(CHANNEL, name="t", type=thread_type)
    assert req["payload"]["type"] == thread_type


@pytest.mark.parametrize("thread_type", [0, 9, 13, "11", 11.0, True])
def test_start_thread_rejects_invalid_types(thread_type):
    with pytest.raises(ThreadError):
        start_thread_request(CHANNEL, name="t", type=thread_type)


# ------------------------------------------------------------ name rules


def test_start_thread_name_is_trimmed():
    req = start_thread_request(CHANNEL, name="  padded  ")
    assert req["payload"]["name"] == "padded"


def test_rename_thread_name_is_trimmed():
    req = rename_thread_request(THREAD, name="\t padded \n")
    assert req["payload"]["name"] == "padded"


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * (NAME_MAX_LENGTH + 1)])
def test_start_thread_rejects_bad_name(bad_name):
    with pytest.raises(ThreadError):
        start_thread_request(CHANNEL, name=bad_name)


@pytest.mark.parametrize("bad_name", ["", "   ", "x" * (NAME_MAX_LENGTH + 1)])
def test_rename_thread_rejects_bad_name(bad_name):
    with pytest.raises(ThreadError):
        rename_thread_request(THREAD, name=bad_name)


@pytest.mark.parametrize(
    "good_name", ["x", "x" * NAME_MIN_LENGTH, "x" * NAME_MAX_LENGTH]
)
def test_start_thread_accepts_boundary_names(good_name):
    req = start_thread_request(CHANNEL, name=good_name)
    assert req["payload"]["name"] == good_name


def test_non_string_name_rejected():
    with pytest.raises(ThreadError):
        start_thread_request(CHANNEL, name=None)


# ------------------------------------------------------ snowflake rules


@pytest.mark.parametrize(
    "value", [0, 1, "0", CHANNEL, MAX_SNOWFLAKE, str(MAX_SNOWFLAKE)]
)
def test_start_thread_accepts_valid_snowflakes(value):
    req = start_thread_request(value, name="ok")
    assert req["path"] == f"/channels/{value}/threads"


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, -(1 << 63), 1 << 63, "abc", "12.5", "", "   ", [], {}],
)
def test_start_thread_rejects_invalid_snowflakes(value):
    with pytest.raises(ThreadError):
        start_thread_request(value, name="ok")


def test_start_thread_rejects_bad_message_id():
    with pytest.raises(ThreadError):
        start_thread_request(CHANNEL, name="x", message_id="not-a-snowflake")


@pytest.mark.parametrize(
    "builder",
    [
        lambda: archive_thread_request("bad"),
        lambda: rename_thread_request("bad", name="x"),
        lambda: list_active_threads_request("bad"),
        lambda: join_thread_request("bad"),
    ],
    ids=["archive", "rename", "list_active", "join"],
)
def test_all_builders_validate_snowflake(builder):
    with pytest.raises(ThreadError):
        builder()


# ------------------------------------------------------- payload detail


def test_archive_thread_explicit_payload():
    req = archive_thread_request(THREAD, archived=False, locked=True)
    assert req["payload"] == {"archived": False, "locked": True}


def test_thread_error_is_value_error():
    assert issubclass(ThreadError, ValueError)
    with pytest.raises(ValueError):
        start_thread_request("bad", name="x")
