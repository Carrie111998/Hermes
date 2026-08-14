"""Adapter-level tests for plugin interaction sends and callback dispatch."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN = "Markdown"
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ParseMode.HTML = "HTML"
    mod.constants.ChatType.PRIVATE = "private"
    mod.error.NetworkError = type("NetworkError", (OSError,), {})
    mod.error.TimedOut = type("TimedOut", (OSError,), {})
    mod.error.BadRequest = type("BadRequest", (Exception,), {})
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)
    sys.modules.setdefault("telegram.error", mod.error)


_ensure_telegram_mock()

from gateway.config import PlatformConfig
from hermes_cli.plugin_interactions import PluginCallbackResult, PluginInlineButton, PluginInteractionReply
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=SimpleNamespace(message_id=99)
    )
    return adapter


class TestTelegramPluginInteractionSend:
    @pytest.mark.asyncio
    async def test_structured_reply_uses_thread_fallback_with_html_and_keyboard(self):
        adapter = _make_adapter()
        metadata = {
            "plugin_parse_mode": "html",
            "plugin_inline_keyboard": [[{"text": "✓ Read", "callback_data": "rd:abc:r"}]],
            "thread_id": "1823812",
        }

        result = await adapter.send("99111810", "<b>Title</b>\n• Point", metadata=metadata)

        assert result.success is True
        adapter._send_message_with_thread_fallback.assert_awaited_once()
        kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
        assert str(kwargs["parse_mode"]).endswith("HTML")
        assert kwargs["reply_markup"] is not None
        assert kwargs.get("message_thread_id") == 1823812


class TestTelegramPluginCallbackDispatch:
    @pytest.mark.asyncio
    async def test_plugin_callback_answers_and_edits_message(self):
        adapter = _make_adapter()
        query = SimpleNamespace(
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat_id="99111810", message_id=12),
            from_user=SimpleNamespace(id="99111810", first_name="Owner"),
        )

        async def on_read_later(data: str, query, adapter=None):
            return PluginCallbackResult(answer_text="Updated", edit_text="✓ <b>Done</b>")

        with patch(
            "hermes_cli.plugins.get_plugin_manager",
        ) as get_mgr:
            mgr = get_mgr.return_value
            mgr.dispatch_telegram_callback = AsyncMock(
                return_value=PluginCallbackResult(answer_text="Updated", edit_text="✓ <b>Done</b>")
            )
            handled = await adapter._handle_plugin_callback_query(query, "rd:abc:r")

        assert handled is True
        query.answer.assert_awaited_once_with(text="Updated")
        query.edit_message_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reserved_prefix_falls_through_to_builtin_handlers(self):
        adapter = _make_adapter()
        adapter._approval_state = {7: "agent:main:telegram:private:1"}
        adapter._is_callback_user_authorized = MagicMock(return_value=True)
        adapter.resume_typing_for_chat = MagicMock()

        query = SimpleNamespace(
            data="ea:once:7",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(
                chat_id="99111810",
                message_id=12,
                message_thread_id=1823812,
                chat=SimpleNamespace(type="private"),
            ),
            from_user=SimpleNamespace(id="99111810", first_name="Owner"),
        )
        update = SimpleNamespace(callback_query=query)
        context = SimpleNamespace()

        with (
            patch("hermes_cli.plugins.get_plugin_manager") as get_mgr,
            patch("tools.approval.resolve_gateway_approval", return_value=1),
        ):
            mgr = get_mgr.return_value
            mgr.dispatch_telegram_callback = AsyncMock(
                return_value=PluginCallbackResult(answer_text="hijacked")
            )
            await adapter._handle_callback_query(update, context)

        mgr.dispatch_telegram_callback.assert_not_awaited()
        query.answer.assert_awaited()
        assert 7 not in adapter._approval_state
