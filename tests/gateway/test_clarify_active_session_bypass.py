"""Regression tests for clarify replies while a gateway session is busy."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, build_session_key


class _ClarifyBypassAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "private"}


def _event(
    text="custom answer",
    *,
    user_id="user1",
    chat_id="12345",
    chat_type="private",
):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=user_id,
        ),
        message_id="msg1",
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


@pytest.mark.asyncio
async def test_active_session_routes_typed_choice_clarify_reply_to_runner_not_busy_queue():
    """Typed text must resolve a pending choice clarify even while the agent is busy.

    Telegram button clarifies keep the adapter session active while the agent
    thread blocks on ``wait_for_response``.  If the adapter only bypasses for
    entries already marked ``awaiting_text``, typed replies to the visible
    multi-choice prompt are handled as busy follow-ups and the clarify wait is
    never resolved.
    """
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("None of those are valid options")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register("clarify-1", session_key, "Pick one", ["A", "B"])
    entry = cm._entries["clarify-1"]
    entry.source_identity = cm.source_identity(event.source)

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()
    assert adapter._pending_messages == {}


@pytest.mark.asyncio
async def test_active_session_bypass_uses_profile_namespaced_key_under_multiplex():
    """Regression for issue #82975: under a named-profile multiplex, the
    adapter's clarify bypass lookup must use the SAME profile-namespaced
    session key that the runner registers pending clarifies under
    (SessionStore._generate_session_key() includes
    profile=self._resolve_profile_for_key(source)), not the legacy
    unnamespaced key. Otherwise the lookup misses, and a user's answer to
    a pending clarify is routed to the busy-session queue instead of
    resolving it -- the turn then hangs until the clarify's 3600s timeout."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("None of those are valid options")

    # A session_store configured for profile multiplexing, matching what
    # the runner's SessionStore._generate_session_key() actually produces.
    session_store = MagicMock()
    session_store._resolve_profile_for_key.return_value = "ops"
    adapter._session_store = session_store

    profile_namespaced_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
        profile="ops",
    )
    # Sanity: the profile-namespaced key really is different from the
    # legacy unnamespaced one -- otherwise this test wouldn't distinguish
    # the fixed behavior from the bug.
    legacy_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    assert profile_namespaced_key != legacy_key

    adapter._active_sessions[profile_namespaced_key] = asyncio.Event()
    # The runner registers the pending clarify under its own
    # profile-namespaced key, exactly as it would in a real multiplexed
    # deployment.
    cm.register("clarify-1", profile_namespaced_key, "Pick one", ["A", "B"])

    await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    adapter._busy_session_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_private_session_does_not_route_other_requester_to_pending_clarify():
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter.config.extra["group_sessions_per_user"] = False
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    owner_event = _event(user_id="owner")
    other_event = _event("unrelated", user_id="other")
    session_key = build_session_key(
        owner_event.source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register(
        "clarify-owner",
        session_key,
        "Pick one",
        ["A", "B"],
        source_identity=cm.source_identity(owner_event.source),
    )

    await adapter.handle_message(other_event)

    adapter._message_handler.assert_not_awaited()
    adapter._busy_session_handler.assert_awaited_once_with(other_event, session_key)


@pytest.mark.asyncio
async def test_active_shared_group_routes_other_member_to_pending_clarify():
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter.config.extra["group_sessions_per_user"] = False
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    owner_event = _event(user_id="owner", chat_id="group-1", chat_type="group")
    member_event = _event(
        "the answer from another member",
        user_id="member",
        chat_id="group-1",
        chat_type="group",
    )
    session_key = build_session_key(
        owner_event.source,
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    entry = cm.register(
        "clarify-shared-group",
        session_key,
        "Pick one",
        ["A", "B"],
        source_identity=cm.source_identity(owner_event.source),
        shared_multi_user_session=True,
    )

    await adapter.handle_message(member_event)

    adapter._message_handler.assert_awaited_once_with(member_event)
    adapter._busy_session_handler.assert_not_awaited()
    assert cm.resolve_text_response_for_session(
        session_key,
        member_event.text,
        source_identity=cm.source_identity(member_event.source),
    ) is True
    assert entry.response == member_event.text


@pytest.mark.asyncio
async def test_active_per_user_group_does_not_route_other_member_to_owner_clarify():
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter.config.extra["group_sessions_per_user"] = True
    adapter._message_handler = AsyncMock(return_value="")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    owner_event = _event(user_id="owner", chat_id="group-1", chat_type="group")
    member_event = _event(
        "unrelated",
        user_id="member",
        chat_id="group-1",
        chat_type="group",
    )
    owner_session_key = build_session_key(
        owner_event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    member_session_key = build_session_key(
        member_event.source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    )
    adapter._active_sessions[member_session_key] = asyncio.Event()
    cm.register(
        "clarify-isolated-group",
        owner_session_key,
        "Pick one",
        ["A", "B"],
        source_identity=cm.source_identity(owner_event.source),
    )

    await adapter.handle_message(member_event)

    adapter._message_handler.assert_not_awaited()
    adapter._busy_session_handler.assert_awaited_once_with(
        member_event,
        member_session_key,
    )


@pytest.mark.asyncio
async def test_active_session_routes_plugin_command_as_command_not_clarify() -> None:
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _ClarifyBypassAdapter()
    adapter._message_handler = AsyncMock(return_value="refreshed")
    adapter._busy_session_handler = AsyncMock(return_value=True)
    event = _event("/octo-refresh")
    session_key = build_session_key(
        event.source,
        group_sessions_per_user=adapter.config.extra.get("group_sessions_per_user", True),
        thread_sessions_per_user=adapter.config.extra.get("thread_sessions_per_user", False),
    )
    adapter._active_sessions[session_key] = asyncio.Event()
    cm.register(
        "clarify-plugin-command",
        session_key,
        "Pick one",
        ["A", "B"],
        source_identity=cm.source_identity(event.source),
    )

    with patch(
        "hermes_cli.commands._iter_plugin_command_entries",
        return_value=[("octo-refresh", "Refresh", "")],
    ):
        await adapter.handle_message(event)

    adapter._message_handler.assert_awaited_once_with(event)
    assert cm.has_pending(session_key) is True
    adapter._busy_session_handler.assert_not_awaited()
