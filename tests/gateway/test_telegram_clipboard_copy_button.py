"""Tests for the Telegram "copy full response to Hermes clipboard" button.

Covers both places a legacy text reply can be split into multiple Telegram
messages: the normal send() chunking path and the edit/streaming-completion
overflow path (_edit_overflow_split).
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported
# ---------------------------------------------------------------------------
def _ensure_telegram_mock():
    """Wire up the minimal mocks required to import TelegramAdapter."""
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

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter(extra=None, *, max_message_length=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    if max_message_length is not None:
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", max_message_length)
    return adapter


def _message(message_id):
    return SimpleNamespace(message_id=message_id)


def _query(data, *, chat_id=12345, thread_id=None, user_id="111"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.message.chat = MagicMock()
    query.message.chat.type = "private"
    query.message.message_thread_id = thread_id
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _update_for(query):
    update = MagicMock()
    update.callback_query = query
    return update


ALLOW_ALL = {"TELEGRAM_ALLOWED_USERS": "*"}


# ===========================================================================
# send() — legacy split path
# ===========================================================================

class TestSendClipboardButtonAttachment:
    @pytest.mark.asyncio
    async def test_single_message_reply_has_no_button_and_no_state(self):
        adapter = _make_adapter()
        adapter._bot.send_message = AsyncMock(return_value=_message(1))

        result = await adapter.send("12345", "short reply", metadata={"notify": True})

        assert result.success is True
        kwargs = adapter._bot.send_message.call_args[1]
        assert "reply_markup" not in kwargs
        assert adapter._clipboard_copy_state == {}

    @pytest.mark.asyncio
    async def test_split_reply_attaches_button_only_to_final_message(self):
        adapter = _make_adapter(max_message_length=None)
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 120)
        sent = []

        async def fake_send_message(**kwargs):
            msg = _message(len(sent) + 1)
            sent.append(kwargs)
            return msg

        adapter._bot.send_message = AsyncMock(side_effect=fake_send_message)

        original = "word " * 80  # forces multiple chunks at MAX_MESSAGE_LENGTH=120
        result = await adapter.send("12345", original, metadata={"notify": True})

        assert result.success is True
        assert len(sent) > 1
        for kwargs in sent[:-1]:
            assert "reply_markup" not in kwargs or kwargs["reply_markup"] is None
        assert sent[-1]["reply_markup"] is not None
        assert len(adapter._clipboard_copy_state) == 1
        token, entry = next(iter(adapter._clipboard_copy_state.items()))
        assert entry["text"] == original
        assert entry["chat_id"] == "12345"

    @pytest.mark.asyncio
    async def test_intermediate_non_final_send_gets_no_button_even_if_split(self):
        """metadata without notify=True is a progress/status send, not the final reply."""
        adapter = _make_adapter()
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 120)
        adapter._bot.send_message = AsyncMock(side_effect=lambda **k: _message(1))

        result = await adapter.send("12345", "word " * 80, metadata={})

        assert result.success is True
        assert adapter._clipboard_copy_state == {}

    @pytest.mark.asyncio
    async def test_callback_data_is_short_opaque_token_not_report_text(self, monkeypatch):
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardButton",
            lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data),
        )
        monkeypatch.setattr(
            "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
            lambda rows: SimpleNamespace(inline_keyboard=rows),
        )
        adapter = _make_adapter()
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 120)
        sent = []

        async def fake_send_message(**kwargs):
            sent.append(kwargs)
            return _message(len(sent))

        adapter._bot.send_message = AsyncMock(side_effect=fake_send_message)
        original = "secret-report-body " * 20
        await adapter.send("12345", original, metadata={"notify": True})

        markup = sent[-1]["reply_markup"]
        button = markup.inline_keyboard[0][0]
        assert button.callback_data.startswith("cc:")
        token = button.callback_data.split(":", 1)[1]
        assert len(button.callback_data.encode("utf-8")) <= 64
        assert "secret-report-body" not in button.callback_data
        assert token in adapter._clipboard_copy_state


# ===========================================================================
# _edit_overflow_split — edit/streaming-completion overflow path
# ===========================================================================

class TestEditOverflowClipboardButtonAttachment:
    @pytest.mark.asyncio
    async def test_finalize_split_attaches_button_to_last_continuation_only(self):
        adapter = _make_adapter()
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 160)
        adapter._bot.edit_message_text = AsyncMock(return_value=True)
        sent = []

        async def fake_send_message(**kwargs):
            sent.append(kwargs)
            return _message(100 + len(sent))

        adapter._bot.send_message = AsyncMock(side_effect=fake_send_message)

        original = "word " * 120
        result = await adapter._edit_overflow_split(
            "12345", "1", original, finalize=True, metadata={"thread_id": "77"},
        )

        assert result.success is True
        assert len(sent) >= 1
        for kwargs in sent[:-1]:
            assert not kwargs.get("reply_markup")
        assert sent[-1]["reply_markup"] is not None
        assert len(adapter._clipboard_copy_state) == 1
        entry = next(iter(adapter._clipboard_copy_state.values()))
        assert entry["text"] == original

    @pytest.mark.asyncio
    async def test_non_finalize_overflow_gets_no_button(self):
        """Mid-stream (finalize=False) previews never attach the button."""
        adapter = _make_adapter()
        object.__setattr__(adapter, "MAX_MESSAGE_LENGTH", 160)
        adapter._bot.edit_message_text = AsyncMock(return_value=True)
        adapter._bot.send_message = AsyncMock(side_effect=lambda **k: _message(1))

        result = await adapter._edit_overflow_split(
            "12345", "1", "word " * 120, finalize=False, metadata={"thread_id": "77"},
        )

        assert result.success is True
        assert adapter._clipboard_copy_state == {}


# ===========================================================================
# _handle_callback_query — cc: callback
# ===========================================================================

class TestClipboardCopyCallback:
    @pytest.mark.asyncio
    async def test_authorized_callback_writes_exact_original_text_and_consumes_token(self):
        adapter = _make_adapter()
        original = "The *complete* unsplit\nresponse text (1/2) [not escaped]"
        adapter._clipboard_copy_state["tok1"] = {
            "text": original, "chat_id": "12345", "thread_id": None, "created": __import__("time").time(),
        }
        query = _query("cc:tok1")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text", return_value=True) as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_called_once_with(original)
        assert "tok1" not in adapter._clipboard_copy_state
        query.answer.assert_called_once()
        assert "copied" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected_and_clipboard_not_written(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": None, "created": 0.0,
        }
        query = _query("cc:tok1")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "999"}, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text") as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_not_called()
        assert "tok1" in adapter._clipboard_copy_state  # untouched, still pending
        assert "not authorized" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_wrong_chat_is_rejected(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "99999", "thread_id": None, "created": __import__("time").time(),
        }
        query = _query("cc:tok1", chat_id=12345)

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text") as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_not_called()
        assert "tok1" in adapter._clipboard_copy_state

    @pytest.mark.asyncio
    async def test_wrong_thread_is_rejected(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": "555", "created": __import__("time").time(),
        }
        query = _query("cc:tok1", chat_id=12345, thread_id=999)

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text") as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_token_handled_cleanly(self):
        adapter = _make_adapter()
        query = _query("cc:does-not-exist")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text") as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_not_called()
        assert "expired" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_expired_token_is_pruned_and_rejected(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": None,
            "created": 0.0,  # far in the past -> expired under any real TTL
        }
        query = _query("cc:tok1")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text") as write_mock:
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        write_mock.assert_not_called()
        assert "tok1" not in adapter._clipboard_copy_state

    @pytest.mark.asyncio
    async def test_already_consumed_token_rejected_on_second_click(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": None, "created": __import__("time").time(),
        }
        query1 = _query("cc:tok1")
        query2 = _query("cc:tok1")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text", return_value=True) as write_mock:
            await adapter._handle_callback_query(_update_for(query1), MagicMock())
            await adapter._handle_callback_query(_update_for(query2), MagicMock())

        assert write_mock.call_count == 1
        assert "expired" in query2.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_clipboard_backend_failure_answers_failure_without_raising(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": None, "created": __import__("time").time(),
        }
        query = _query("cc:tok1")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text", return_value=False):
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        assert "tok1" not in adapter._clipboard_copy_state  # still consumed
        assert "unavailable" in query.answer.call_args[1]["text"].lower()

    @pytest.mark.asyncio
    async def test_clipboard_backend_raises_is_non_fatal(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["tok1"] = {
            "text": "secret", "chat_id": "12345", "thread_id": None, "created": __import__("time").time(),
        }
        query = _query("cc:tok1")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("hermes_cli.clipboard.write_clipboard_text", side_effect=RuntimeError("boom")):
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        assert "unavailable" in query.answer.call_args[1]["text"].lower()


# ===========================================================================
# Bounded state
# ===========================================================================

class TestClipboardCopyStateBounds:
    def test_state_is_bounded_by_max_pending(self):
        adapter = _make_adapter()
        for i in range(adapter._CLIPBOARD_COPY_MAX_PENDING + 10):
            adapter._register_clipboard_copy(f"text-{i}", chat_id="1", thread_id=None)
        assert len(adapter._clipboard_copy_state) <= adapter._CLIPBOARD_COPY_MAX_PENDING

    def test_expired_entries_are_pruned_on_register(self):
        adapter = _make_adapter()
        adapter._clipboard_copy_state["old"] = {
            "text": "x", "chat_id": "1", "thread_id": None, "created": 0.0,
        }
        adapter._register_clipboard_copy("new text", chat_id="1", thread_id=None)
        assert "old" not in adapter._clipboard_copy_state


# ===========================================================================
# Existing callbacks unaffected by the new cc: branch
# ===========================================================================

class TestOtherCallbacksUnaffected:
    @pytest.mark.asyncio
    async def test_approval_callback_still_dispatches_normally(self):
        adapter = _make_adapter()
        adapter._approval_state[5] = "agent:main:telegram:group:12345:99"
        query = _query("ea:once:5")

        with patch.dict(os.environ, ALLOW_ALL, clear=False), \
             patch("tools.approval.resolve_gateway_approval", return_value=1):
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        assert 5 not in adapter._approval_state
        query.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_prompt_callback_still_dispatches_normally(self, tmp_path):
        adapter = _make_adapter()
        query = _query("update_prompt:y")

        with patch("hermes_constants.get_hermes_home", return_value=tmp_path), \
             patch.dict(os.environ, ALLOW_ALL, clear=False):
            await adapter._handle_callback_query(_update_for(query), MagicMock())

        assert (tmp_path / ".update_response").read_text() == "y"
