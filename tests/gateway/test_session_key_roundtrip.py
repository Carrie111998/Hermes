"""Regression: ``parse_session_key`` must be the inverse of ``build_session_key``.

A session key is colon-delimited and its grammar is platform-specific, so a
positional ``split(":")`` is lossy on real keys:

* a Matrix room id is ``!localpart:homeserver``, so
  ``agent:main:matrix:group:!room:example.org`` split positionally yields
  ``chat_id == "!room"`` — a room that does not exist — and silently discards
  the homeserver;
* ``build_session_key`` inserts the Slack workspace id BETWEEN the chat-type
  slot and the chat id, so on a scoped Slack key the chat-id slot holds the
  workspace.

The contract these tests pin: for every shape ``build_session_key`` emits,
parsing the key it built recovers the platform, chat type, chat id, workspace
scope and profile it was built from.
"""

import pytest

from gateway.config import Platform
from gateway.session import SessionSource, build_session_key, parse_session_key


def _source(**kwargs) -> SessionSource:
    kwargs.setdefault("chat_type", "group")
    return SessionSource(**kwargs)


ROUND_TRIPS = [
    pytest.param(
        _source(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_type="group",
            user_id="@user:example.org",
        ),
        "alpha",
        id="matrix-group-colon-room-id-and-colon-user-id",
    ),
    pytest.param(
        _source(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_type="dm",
            user_id="@user:example.org",
        ),
        None,
        id="matrix-dm-colon-room-id",
    ),
    pytest.param(
        _source(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_type="thread",
            thread_id="$event:example.org",
        ),
        "alpha",
        id="matrix-thread-colon-room-and-colon-thread-id",
    ),
    pytest.param(
        _source(
            platform=Platform.SLACK,
            chat_id="C0CHANNEL",
            chat_type="group",
            scope_id="T0WORKSPACE",
            user_id="U0USER",
        ),
        None,
        id="slack-scoped-group",
    ),
    pytest.param(
        _source(
            platform=Platform.SLACK,
            chat_id="D0DIRECT",
            chat_type="dm",
            scope_id="T0WORKSPACE",
        ),
        "alpha",
        id="slack-scoped-dm",
    ),
    pytest.param(
        _source(platform=Platform.SLACK, chat_id="D0DIRECT", chat_type="dm"),
        None,
        id="slack-unscoped-dm",
    ),
    pytest.param(
        _source(
            platform=Platform.DISCORD,
            chat_id="123456",
            chat_type="group",
            user_id="789",
        ),
        None,
        id="discord-group-per-user",
    ),
    pytest.param(
        _source(
            platform=Platform.TELEGRAM,
            chat_id="-100123",
            chat_type="thread",
            thread_id="42",
        ),
        "alpha",
        id="telegram-forum-topic",
    ),
]


@pytest.mark.parametrize("source,profile", ROUND_TRIPS)
def test_parse_is_the_inverse_of_build(source, profile):
    key = build_session_key(source, profile=profile)
    parsed = parse_session_key(key)

    assert parsed is not None, f"{key!r} did not parse"
    assert parsed["platform"] == source.platform.value
    assert parsed["chat_id"] == source.chat_id, (
        f"{key!r} -> chat_id {parsed['chat_id']!r}; delivering there is "
        "delivering to the wrong chat"
    )
    # ``main`` is a namespace literal, not a profile — it is reported as None so
    # it can be handed straight to the profile-aware resolvers.
    assert parsed["profile"] == profile
    if source.scope_id:
        assert parsed.get("scope_id") == source.scope_id


@pytest.mark.parametrize("source,profile", ROUND_TRIPS)
def test_chat_type_slot_round_trips(source, profile):
    """The chat-type slot survives, including Discord's prospective-thread
    rewrite of ``group`` to ``thread``."""
    key = build_session_key(source, profile=profile)
    assert parse_session_key(key)["chat_type"] == key.split(":")[2 + 1]


def test_thread_id_only_where_the_grammar_is_unambiguous():
    """A 6th token in a GROUP key may be a participant id, not a thread — so
    ``thread_id`` is reported only for ``dm``/``thread``."""
    group = build_session_key(
        _source(platform=Platform.DISCORD, chat_id="chan", chat_type="group", user_id="u1")
    )
    assert "thread_id" not in parse_session_key(group)

    thread = build_session_key(
        _source(
            platform=Platform.MATRIX,
            chat_id="!room:example.org",
            chat_type="thread",
            thread_id="$evt:example.org",
        )
    )
    assert parse_session_key(thread)["thread_id"] == "$evt:example.org"


@pytest.mark.parametrize(
    "not_a_key",
    [
        "",
        "sess_abc123",                    # raw api_server session id
        "agent:main:matrix:dm",           # too short
        "agent::matrix:dm:!room",         # empty namespace slot
        "chatcmpl-xyz",
    ],
)
def test_non_keys_return_none(not_a_key):
    """``_inject_watch_notification`` uses ``None`` here to tell a gateway
    session key from a raw api_server session id it must self-post to."""
    assert parse_session_key(not_a_key) is None
