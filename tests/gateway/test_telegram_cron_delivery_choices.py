"""Telegram cron delivery-choice buttons (#78999)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

from cron.delivery_choices import (  # noqa: E402
    clear_delivery_choices,
    register_delivery_choices,
)
from gateway.config import PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    adapter._send_message_with_thread_fallback = AsyncMock(
        return_value=MagicMock(message_id=99)
    )
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    adapter.handle_message = AsyncMock()
    return adapter


def _callback_update(data: str, text: str = "preview"):
    query = MagicMock()
    query.data = data
    query.message.text = text
    query.message.chat_id = 123
    query.message.chat.type = "private"
    query.message.message_thread_id = None
    query.from_user.id = 7
    query.from_user.first_name = "Stefan"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


class TestSendDeliveryChoices(unittest.IsolatedAsyncioTestCase):
    async def test_renders_preview_text(self):
        adapter = _make_adapter()
        result = await adapter.send_delivery_choices(
            "123",
            "Preview body",
            ["vis bilag", "spring over"],
            "deliv1",
        )
        self.assertTrue(result.success)
        kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
        self.assertEqual(kwargs["text"], "Preview body")
        self.assertIsNotNone(kwargs.get("reply_markup"))


class TestDeliveryChoiceCallback(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear_delivery_choices()

    def tearDown(self):
        clear_delivery_choices()

    async def test_tap_injects_choice_as_user_turn(self):
        register_delivery_choices("d1", ["vis bilag", "spring over"], "job-1")
        adapter = _make_adapter()
        update, query = _callback_update("cd:d1:0")
        await adapter._handle_callback_query(update, None)
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "vis bilag")
        query.answer.assert_awaited()

    async def test_stale_tap_is_visible_and_does_not_inject(self):
        adapter = _make_adapter()
        update, query = _callback_update("cd:missing:0")
        await adapter._handle_callback_query(update, None)
        adapter.handle_message.assert_not_awaited()
        answer_text = query.answer.await_args.kwargs.get("text") or ""
        if not answer_text and query.answer.await_args.args:
            answer_text = query.answer.await_args.args[0]
        self.assertIn("expired", answer_text.lower())


if __name__ == "__main__":
    unittest.main()
