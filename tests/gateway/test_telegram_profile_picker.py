"""Tests for Telegram profile picker (multiplexed gateways)."""

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
from plugins.platforms.telegram.adapter import TelegramAdapter


def _make_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


class TestTelegramProfilePicker:
    @pytest.mark.asyncio
    async def test_send_profile_picker_marks_current(self):
        adapter = _make_adapter()
        sent = {}

        async def mock_send_message(**kwargs):
            sent.update(kwargs)
            return SimpleNamespace(message_id=201)

        adapter._bot.send_message = AsyncMock(side_effect=mock_send_message)

        profiles = [
            {"value": "default", "label": "default", "is_current": True},
            {"value": "social", "label": "social", "is_current": False},
            {"value": "coder", "label": "coder", "is_current": False},
        ]

        result = await adapter.send_profile_picker(
            chat_id="12345",
            profiles=profiles,
            current_profile="default",
            session_key="s",
            on_profile_selected=AsyncMock(),
            metadata={"thread_id": "99999"},
        )

        assert result.success is True
        # State must be populated so the callback can resolve the picked index.
        state = adapter._profile_picker_state.get("12345")
        assert state is not None
        assert len(state["profiles"]) == 3
        assert state["current_profile"] == "default"
        # Current profile name is surfaced in the message body.
        assert "default" in sent["text"]
        assert sent["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_send_profile_picker_empty_profiles(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock()

        result = await adapter.send_profile_picker(
            chat_id="12345",
            profiles=[],
            current_profile="default",
            session_key="s",
            on_profile_selected=AsyncMock(),
        )

        assert result.success is False
        # Empty picker must NOT call send_message.
        adapter._bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_profile_picker_callback_invokes_on_selected(self):
        adapter = _make_adapter()
        callback = AsyncMock(return_value="✅ Switched to social")
        adapter._profile_picker_state["12345"] = {
            "profiles": [
                {"value": "default", "label": "default", "is_current": True},
                {"value": "social", "label": "social", "is_current": False},
            ],
            "current_profile": "default",
            "session_key": "s",
            "on_profile_selected": callback,
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "pp:1"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 8281634331
        query.from_user.first_name = "Lucas"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        # Authorized user (matches _is_callback_user_authorized's expected id).
        adapter._is_callback_user_authorized = MagicMock(return_value=True)

        await adapter._handle_profile_picker_callback(query, "pp:1", "12345")

        # Callback was invoked with the right profile name.
        callback.assert_awaited_once()
        args = callback.await_args.args
        assert args[1] == "social"
        # Picker state was cleared.
        assert "12345" not in adapter._profile_picker_state
        # The original message was edited to the result.
        query.edit_message_text.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_profile_picker_callback_blocks_unauthorized(self):
        adapter = _make_adapter()
        callback = AsyncMock(return_value="should not be called")
        adapter._profile_picker_state["12345"] = {
            "profiles": [{"value": "social", "label": "social", "is_current": False}],
            "current_profile": "default",
            "session_key": "s",
            "on_profile_selected": callback,
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "pp:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.from_user = MagicMock()
        query.from_user.id = 9999999  # Not the allowed user.
        query.from_user.first_name = "Stranger"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        adapter._is_callback_user_authorized = MagicMock(return_value=False)

        await adapter._handle_profile_picker_callback(query, "pp:0", "12345")

        callback.assert_not_awaited()
        # State preserved — the legitimate user can still tap their picker.
        assert "12345" in adapter._profile_picker_state

    @pytest.mark.asyncio
    async def test_profile_picker_callback_expired_state(self):
        adapter = _make_adapter()
        # No prior picker state.

        query = AsyncMock()
        query.data = "pp:0"
        query.message = MagicMock()
        query.answer = AsyncMock()

        await adapter._handle_profile_picker_callback(query, "pp:0", "12345")

        # Should answer with an expiration hint and not crash.
        query.answer.assert_awaited_once()
        answer_text = query.answer.await_args.kwargs.get("text", "")
        assert "expired" in answer_text.lower()

    @pytest.mark.asyncio
    async def test_profile_picker_callback_invalid_index(self):
        adapter = _make_adapter()
        adapter._profile_picker_state["12345"] = {
            "profiles": [{"value": "social", "label": "social", "is_current": False}],
            "current_profile": "default",
            "session_key": "s",
            "on_profile_selected": AsyncMock(),
            "msg_id": 42,
        }

        query = AsyncMock()
        query.data = "pp:99"  # out of range
        query.message = MagicMock()
        query.answer = AsyncMock()

        adapter._is_callback_user_authorized = MagicMock(return_value=True)

        await adapter._handle_profile_picker_callback(query, "pp:99", "12345")

        query.answer.assert_awaited_once()
        answer_text = query.answer.await_args.kwargs.get("text", "")
        assert "invalid" in answer_text.lower()