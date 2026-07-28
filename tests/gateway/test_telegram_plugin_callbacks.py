"""Tests for Telegram-only plugin callback handlers and legacy callback routing."""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return

    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})

    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(data):
    query = SimpleNamespace(
        data=data,
        message=SimpleNamespace(
            chat_id=12345,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
            message_id=77,
            text="Original message",
        ),
        from_user=SimpleNamespace(id=12345, first_name="Tester"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return query


def _make_update(query):
    return SimpleNamespace(callback_query=query)


@pytest.mark.asyncio
async def test_registered_plugin_callback_edits_message_with_returned_keyboard(monkeypatch):
    adapter = _make_adapter()
    query = _make_query("klib:page:2")
    keyboard = object()
    seen = {}

    async def handler(callback_data, chat_id):
        seen.update(callback_data=callback_data, chat_id=chat_id)
        return "Page 2", keyboard

    monkeypatch.setattr(
        "hermes_cli.plugins.get_plugin_callback_handler",
        lambda data: handler if data.startswith("klib:") else None,
    )

    await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    assert seen == {"callback_data": "klib:page:2", "chat_id": 12345}
    query.edit_message_text.assert_awaited_once_with(
        text="Page 2",
        reply_markup=keyboard,
    )


@pytest.mark.asyncio
async def test_telegram_send_attaches_plugin_keyboard_to_outgoing_message():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=101))
    keyboard = object()

    result = await adapter.send("12345", "Page 2", reply_markup=keyboard)

    assert result.success is True
    assert adapter._bot.send_message.await_args.kwargs["reply_markup"] is keyboard


@pytest.mark.asyncio
async def test_model_command_still_sends_picker_keyboard(monkeypatch, tmp_path):
    """The real /model picker path still sends an inline keyboard."""
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=101)
    )

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner._session_model_overrides = {}
    runner._running_agents = {}
    runner._normalize_source_for_session_key = lambda source: source
    runner._session_key_for_source = lambda source: "telegram-session"
    runner._thread_metadata_for_source = lambda *args, **kwargs: None
    runner._reply_anchor_for_event = lambda *args, **kwargs: None

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setattr("gateway.run._hermes_home", hermes_home)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"model": {"default": "gpt-4", "provider": "openai"}},
    )
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_picker_providers",
        lambda **kwargs: [
            {
                "slug": "openai",
                "name": "OpenAI",
                "total_models": 1,
                "models": ["gpt-4"],
                "is_current": True,
            }
        ],
    )

    event = MessageEvent(
        text="/model",
        message_type=MessageType.TEXT,
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="12345", chat_type="dm"),
    )

    result = await runner._handle_model_command(event)

    assert result is None
    assert adapter._bot.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_unregistered_plugin_callback_falls_through_without_exception(monkeypatch):
    adapter = _make_adapter()
    query = _make_query("unknown:action")
    lookup = MagicMock(return_value=None)
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_callback_handler", lookup)

    await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    lookup.assert_called_once_with("unknown:action")
    query.answer.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback_data",
    ["mp:openai", "mpg:openai", "mpv:1", "mm:0", "mc:0", "mb", "mx", "mg:0"],
)
async def test_model_picker_prefix_still_routes_to_original_handler(
    monkeypatch, callback_data
):
    from hermes_cli.plugins import _ensure_plugins_discovered
    mgr = _ensure_plugins_discovered()
    async def fake_handler(data, chat_id):
        return "Intercepted", None
    mgr._plugin_callback_handlers["openai"] = {
        "handler": fake_handler,
        "plugin": "test-plugin",
    }

    adapter = _make_adapter()
    query = _make_query(callback_data)
    original = AsyncMock()
    adapter._handle_model_picker_callback = original

    await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    original.assert_awaited_once_with(query, callback_data, "12345")


@pytest.mark.asyncio
async def test_gmail_triage_prefix_still_routes_to_original_handler(monkeypatch):
    adapter = _make_adapter()
    query = _make_query("gt:send:message-1")
    original = AsyncMock()
    adapter._handle_gmail_triage_callback = original
    plugin_lookup = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_callback_handler", plugin_lookup)

    await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    original.assert_awaited_once()
    assert original.await_args.args[:2] == (query, "gt:send:message-1")
    plugin_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_exec_approval_prefix_still_routes_to_original_handler(monkeypatch):
    adapter = _make_adapter()
    adapter._approval_state[1] = "session-1"
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    adapter.resume_typing_for_chat = MagicMock()
    query = _make_query("ea:once:1")
    plugin_lookup = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_callback_handler", plugin_lookup)

    with patch("tools.approval.resolve_gateway_approval", return_value=1):
        await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    assert "session-1" not in adapter._approval_state.values()
    assert "Approved once" in query.answer.await_args.kwargs["text"]
    plugin_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_slash_confirm_prefix_still_routes_to_original_handler(monkeypatch):
    adapter = _make_adapter()
    adapter._slash_confirm_state["confirm-1"] = "session-1"
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    query = _make_query("sc:once:confirm-1")
    plugin_lookup = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_callback_handler", plugin_lookup)

    with patch("tools.slash_confirm.resolve", new=AsyncMock(return_value=None)):
        await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    assert "confirm-1" not in adapter._slash_confirm_state
    assert "Approved once" in query.answer.await_args.kwargs["text"]
    plugin_lookup.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verb", "script_name", "label"),
    [
        ("send", "send-draft.sh", "sent draft"),
        ("spam", "spam.sh", "marked spam"),
    ],
)
async def test_gmail_triage_approve_reject_actions_remain_intact(
    tmp_path, monkeypatch, verb, script_name, label
):
    """Gmail's approve/reject-style buttons still execute their scripts."""
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    query = _make_query(f"gt:{verb}:message-1")
    script = tmp_path / ".hermes" / "scripts" / "gmail-triage" / script_name
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    plugin_lookup = MagicMock(return_value=AsyncMock())
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_callback_handler", plugin_lookup)

    proc = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    with patch(
        "plugins.platforms.telegram.adapter.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ) as spawn:
        await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    spawn.assert_awaited_once_with(
        str(script),
        "message-1",
        *(["email"] if verb == "mute" else []),
        stdout=__import__("asyncio").subprocess.PIPE,
        stderr=__import__("asyncio").subprocess.PIPE,
    )
    assert label in query.answer.await_args.kwargs["text"]
    query.edit_message_text.assert_awaited_once()
    assert query.edit_message_text.await_args.kwargs["reply_markup"] is None
    plugin_lookup.assert_not_called()


@pytest.mark.asyncio
async def test_model_picker_callback_through_main_dispatch_still_selects_model():
    """The dispatcher still reaches the model picker selection path."""
    adapter = _make_adapter()
    callback = AsyncMock(return_value="Switched to gpt-5")
    adapter._model_picker_state["12345"] = {
        "providers": [{"slug": "openai", "name": "OpenAI", "total_models": 1}],
        "current_model": "old-model",
        "current_provider": "openai",
        "session_key": "session-1",
        "on_model_selected": callback,
        "selected_provider": "openai",
        "model_list": ["gpt-5"],
        "msg_id": 77,
    }
    query = _make_query("mm:0")

    with patch("hermes_cli.plugins.get_plugin_callback_handler", return_value=None):
        await adapter._handle_callback_query(_make_update(query), SimpleNamespace())

    callback.assert_awaited_once_with("12345", "gpt-5", "openai")
    assert "12345" not in adapter._model_picker_state
    assert "gpt\\-5" in query.edit_message_text.await_args.kwargs["text"]
