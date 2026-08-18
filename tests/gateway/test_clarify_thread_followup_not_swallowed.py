"""Pending clarify prompts consume the requester's next message as the answer.

Native controls are optional input conveniences. Arbitrary prose from the
bound requester resolves the active clarify instead of falling into busy-run
handling, where an interrupt cannot wake the worker blocked on Event.wait.
"""

from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource


SESSION_KEY = "agent:main:slack:dm:D123:1111.2222"


class _StubAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.SLACK)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="m1")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "im"}




def _event(text):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id="D123",
            chat_type="dm",
            user_id="U1",
            thread_id="1111.2222",
        ),
        message_id="msg1",
    )


def _clear_clarify_state():
    from tools import clarify_gateway as cm

    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _make_runner(adapter):
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._startup_restore_in_progress = False
    runner._scale_to_zero_note_real_inbound = lambda: None
    runner._is_user_authorized = lambda source: True
    runner._session_key_for_source = lambda source: SESSION_KEY
    runner._adapter_for_source = lambda source: adapter
    runner._update_prompt_pending = {}
    return runner


async def _dispatch(runner, event):
    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        return await runner._handle_message(event)


@pytest.mark.asyncio
async def test_thread_prose_resolves_native_multi_choice_clarify():
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    event = _event("just checking the visual UI, no need to pass any data")
    cm.register(
        "cl-native",
        SESSION_KEY,
        "Pick a UI variant",
        ["buttons", "dropdown"],
        source_identity=cm.source_identity(event.source),
    )

    result = await _dispatch(runner, event)

    assert result == ""
    with cm._lock:
        entry = cm._entries.get("cl-native")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == "just checking the visual UI, no need to pass any data"


@pytest.mark.asyncio
async def test_prose_still_accepted_after_other_flips_text_capture():
    """After the user taps 'Other', free text IS the answer — must resolve."""
    _clear_clarify_state()
    from tools import clarify_gateway as cm

    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    event = _event("a carousel actually")
    cm.register(
        "cl-other",
        SESSION_KEY,
        "Pick a UI variant",
        ["buttons", "dropdown"],
        source_identity=cm.source_identity(event.source),
    )
    assert cm.mark_awaiting_text("cl-other") is True

    result = await _dispatch(runner, event)

    assert result == ""
    with cm._lock:
        entry = cm._entries.get("cl-other")
    assert entry is not None
    assert entry.event.is_set()
    assert entry.response == "a carousel actually"


@pytest.mark.asyncio
async def test_plugin_command_dispatches_while_clarify_run_is_active():
    _clear_clarify_state()
    adapter = _StubAdapter()
    runner = _make_runner(adapter)
    is_running = patch.object(runner, "_is_session_running", return_value=True)
    runner.config = SimpleNamespace(quick_commands={})  # type: ignore[assignment]
    emit_collect = AsyncMock(return_value=[])
    runner.hooks = SimpleNamespace(emit_collect=emit_collect)  # type: ignore[assignment]
    runner._check_slash_access = lambda source, canonical_cmd: None
    handler = AsyncMock(return_value="refreshed")
    event = _event("/octo-refresh")

    with (
        is_running,
        patch(
            "hermes_cli.commands._iter_plugin_command_entries",
            return_value=[("octo-refresh", "Refresh", "")],
        ),
        patch(
            "hermes_cli.plugins.get_plugin_command_handler",
            return_value=handler,
        ),
    ):
        result = await runner._handle_message(event)

    assert result == "refreshed"
    handler.assert_awaited_once_with("")
    emit_collect.assert_awaited_once()


def test_prospective_thread_is_shared_when_thread_policy_is_shared():
    from gateway.session import is_shared_multi_user_session

    source = replace(
        SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            chat_type="channel",
            user_id="owner",
        ),
        prospective_thread_id="thread-1",
    )

    assert is_shared_multi_user_session(
        source,
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
    ) is True

