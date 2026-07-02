"""Tests for the TickTick floating-task nudge inline-button callback (tt:*).

The bug these guard against: when Telegram moved into plugins/platforms/telegram,
the tt: dispatch was dropped, so tapping Tomorrow/Today/Keep/Dismiss on a floating
task fell through to the bare update_prompt: return with no query.answer() — the
button shimmered forever and no side effect ran.
"""

import sys
from pathlib import Path
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

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    # Authorize everyone for these tests unless a case overrides it.
    adapter._is_callback_user_authorized = MagicMock(return_value=True)
    return adapter


def _make_query(data):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.from_user = MagicMock()
    query.from_user.id = 8756311637
    query.from_user.first_name = "Michael"
    query.message = MagicMock()
    query.message.text_html = "  • BMW Service"
    query.message.chat_id = 8756311637
    query.message.chat = MagicMock()
    query.message.chat.type = "private"
    query.message.message_thread_id = None
    return query


class _FakeProc:
    def __init__(self, rc, out=b"", err=b""):
        self.returncode = rc
        self._out = out
        self._err = err

    async def communicate(self):
        return self._out, self._err


@pytest.mark.asyncio
async def test_dismiss_answers_and_completes():
    """tt:x → ack immediately (kills shimmer), run helper, edit card with result."""
    adapter = _make_adapter()
    query = _make_query("tt:x:69e5deb889a9d1c36b0de909")

    fake = _FakeProc(0, out=b"dismissed (marked complete)\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)) as spawn, \
         patch("pathlib.Path.exists", return_value=True):
        await adapter._handle_ticktick_callback(
            query, query.data,
            query_chat_id=8756311637, query_chat_type="private",
            query_thread_id=None, query_user_name="Michael",
        )

    # Shimmer killer: query.answer must be called.
    assert query.answer.await_count >= 1
    # Helper invoked with action + task_id as the trailing argv.
    argv = spawn.call_args[0]
    assert argv[-2:] == ("x", "69e5deb889a9d1c36b0de909")
    # Final card carries the success label and no keyboard.
    final = query.edit_message_text.await_args_list[-1]
    assert "dismissed (marked complete)" in final.kwargs["text"]
    assert final.kwargs["reply_markup"] is None
    assert "✅" in final.kwargs["text"]


@pytest.mark.asyncio
async def test_unauthorized_is_rejected():
    adapter = _make_adapter()
    adapter._is_callback_user_authorized = MagicMock(return_value=False)
    query = _make_query("tt:x:abc123")

    with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        await adapter._handle_ticktick_callback(
            query, query.data,
            query_chat_id=1, query_chat_type="private",
            query_thread_id=None, query_user_name="Stranger",
        )

    query.answer.assert_awaited()  # answered with the ⛔ toast
    spawn.assert_not_called()      # no side effect for unauthorized taps


@pytest.mark.asyncio
async def test_helper_failure_shows_error_card():
    adapter = _make_adapter()
    query = _make_query("tt:d:abc123")

    fake = _FakeProc(1, err=b"RuntimeError: TickTick POST -> 404\n")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)), \
         patch("pathlib.Path.exists", return_value=True):
        await adapter._handle_ticktick_callback(
            query, query.data,
            query_chat_id=1, query_chat_type="private",
            query_thread_id=None, query_user_name="Michael",
        )

    final = query.edit_message_text.await_args_list[-1]
    assert "❌" in final.kwargs["text"]
    assert "404" in final.kwargs["text"]


@pytest.mark.asyncio
async def test_unknown_action_rejected():
    adapter = _make_adapter()
    query = _make_query("tt:zz:abc123")

    with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        await adapter._handle_ticktick_callback(
            query, query.data,
            query_chat_id=1, query_chat_type="private",
            query_thread_id=None, query_user_name="Michael",
        )

    query.answer.assert_awaited()
    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_routes_tt_prefix():
    """The main callback dispatcher must route tt:* to the handler (not fall through)."""
    adapter = _make_adapter()
    query = _make_query("tt:k:abc123")
    update = MagicMock()
    update.callback_query = query

    adapter._handle_ticktick_callback = AsyncMock()
    await adapter._handle_callback_query(update, MagicMock())
    adapter._handle_ticktick_callback.assert_awaited_once()
