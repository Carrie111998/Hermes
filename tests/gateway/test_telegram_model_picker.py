"""Tests for Telegram model picker thread fallback."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter, _model_picker_labels


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestTelegramModelPicker:
    def test_model_labels_preserve_provider_prefix_only_for_collisions(self):
        model_ids = [
            "cc/claude-opus-5",
            "kr/claude-opus-5",
            "gpt-5.6-sol",
        ]

        assert _model_picker_labels(model_ids) == [
            "cc/claude-opus-5",
            "kr/claude-opus-5",
            "gpt-5.6-sol",
        ]

    def test_model_keyboard_uses_full_labels_for_collisions(self, monkeypatch):
        import plugins.platforms.telegram.adapter as tg

        built = []

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kwargs):
                built.append((text, callback_data))

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        adapter = _make_adapter()
        adapter._build_model_keyboard(
            ["cc/claude-opus-5", "kr/claude-opus-5", "gpt-5.6-sol"],
            0,
        )

        assert built[:3] == [
            ("cc/claude-opus-5", "mm:0"),
            ("kr/claude-opus-5", "mm:1"),
            ("gpt-5.6-sol", "mm:2"),
        ]

    def test_model_keyboard_disambiguates_colliding_truncated_labels(self, monkeypatch):
        import plugins.platforms.telegram.adapter as tg

        built = []

        class _RecordingButton:
            def __init__(self, text, callback_data=None, **kwargs):
                built.append((text, callback_data))

        class _RecordingMarkup:
            def __init__(self, rows):
                self.inline_keyboard = rows

        monkeypatch.setattr(tg, "InlineKeyboardButton", _RecordingButton)
        monkeypatch.setattr(tg, "InlineKeyboardMarkup", _RecordingMarkup)

        model_ids = [
            "provider-" + "x" * 28 + "a/claude-opus-5-" + "y" * 40,
            "provider-" + "x" * 28 + "b/claude-opus-5-" + "y" * 40,
        ]

        _make_adapter()._build_model_keyboard(model_ids, 0)

        assert built[:2] == [
            (model_ids[0][:35] + "...", "mm:0"),
            (model_ids[1][:32] + "...[1]", "mm:1"),
        ]
        assert built[0][0] != built[1][0]

    def test_model_picker_callback_receives_original_colliding_id(self):
        import asyncio

        callback_args = []

        async def on_model_selected(*args):
            callback_args.append(args)
            return "switched"

        adapter = _make_adapter()
        adapter._model_picker_state["12345"] = {
            "providers": [],
            "current_model": "old",
            "current_provider": "provider",
            "session_key": "s",
            "on_model_selected": on_model_selected,
            "selected_provider": "custom:api",
            "model_list": ["cc/claude-opus-5", "kr/claude-opus-5"],
            "msg_id": 42,
        }
        query = AsyncMock()
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        asyncio.run(adapter._handle_model_picker_callback(query, "mm:1", "12345"))

        assert callback_args == [("12345", "kr/claude-opus-5", "custom:api")]

    @pytest.mark.asyncio
    async def test_send_model_picker_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=101)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        result = await adapter.send_model_picker(
            chat_id="12345",
            providers=[
                {"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}
            ],
            current_model="model_1",
            current_provider="provider_one",
            session_key="s",
            on_model_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        assert "MARKDOWN_V2" in repr(sent["parse_mode"])
        assert "provider\\_one" in sent["text"]
        assert "`model_1`" in sent["text"]

    @pytest.mark.asyncio
    async def test_back_button_escapes_dynamic_provider_label(self):
        adapter = _make_adapter()
        adapter._model_picker_state["12345"] = {
            "providers": [{"slug": "provider_one", "name": "Provider One", "total_models": 1, "is_current": True}],
            "current_model": "model_1",
            "current_provider": "provider_one",
            "session_key": "s",
            "on_model_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "mb"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        await adapter._handle_model_picker_callback(query, "mb", "12345")

        edit_kwargs = query.edit_message_text.call_args[1]
        assert "MARKDOWN_V2" in repr(edit_kwargs["parse_mode"])
        assert "provider\\_one" in edit_kwargs["text"]
        assert "`model_1`" in edit_kwargs["text"]


