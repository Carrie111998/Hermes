"""Tests for Telegram inline keyboard clarify buttons.

Mirrors test_telegram_approval_buttons.py for the new ``send_clarify`` and
``cl:`` callback dispatch added in feat/clarify-gateway-buttons.
"""

import os
import inspect
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Telegram mock so TelegramAdapter can be imported (mirrors
# test_telegram_approval_buttons.py)
# ---------------------------------------------------------------------------
from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter(extra=None):
    config = PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._wait_entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


def _register_bound(
    clarify_id="cidA",
    *,
    user_id="777",
    chat_id="12345",
    thread_id=None,
    session_id="session-1",
    active=None,
    choices=None,
):
    from tools import clarify_gateway as cm

    active = active if active is not None else {"session_id": session_id}
    route_lock = active.setdefault("_lock", threading.RLock())

    def _active_session_transaction(action):
        with route_lock:
            if active["session_id"] != session_id:
                return False
            return action()

    return cm.register(
        clarify_id,
        "sk-cb",
        "Pick",
        choices or ["red", "green", "blue"],
        origin=cm.ClarifyOrigin(user_id, chat_id, thread_id),
        session_id=session_id,
        active_session_transaction=_active_session_transaction,
    )


# ===========================================================================
# send_clarify — render
# ===========================================================================

class TestTelegramSendClarify:
    """Verify the rendered prompt has buttons or none, and stores state."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_renders_buttons_and_other(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 100
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        entry = _register_bound("cid1", choices=["alpha", "beta", "gamma"])
        result = await adapter.send_clarify(
            chat_id="12345",
            question="Which option?",
            choices=["alpha", "beta", "gamma"],
            clarify_id="cid1",
            session_key="sk1",
            binding=entry.binding,
        )

        assert result.success is True
        assert result.message_id == "100"

        kwargs = adapter._bot.send_message.call_args[1]
        assert kwargs["chat_id"] == 12345
        assert "Which option?" in kwargs["text"]
        # Full option text rendered in the message body (not just buttons)
        assert "1. alpha" in kwargs["text"]
        assert "2. beta" in kwargs["text"]
        assert "3. gamma" in kwargs["text"]
        # InlineKeyboardMarkup with N+1 buttons (3 choices + Other)
        markup = kwargs["reply_markup"]
        assert markup is not None
        # Binding lifecycle is owned only by the primitive.
        assert not hasattr(adapter, "_clarify_state")


        # The button label should be short ("1"), not the long choice
        # (we can't inspect mock button labels directly, but the send
        # succeeded — old truncation code could raise on edge cases)

    @pytest.mark.asyncio
    async def test_html_escapes_question(self):
        adapter = _make_adapter()
        mock_msg = MagicMock()
        mock_msg.message_id = 103
        adapter._bot.send_message = AsyncMock(return_value=mock_msg)

        entry = _register_bound("cid5", choices=["x"])
        await adapter.send_clarify(
            chat_id="12345",
            question="<script>alert(1)</script>",
            choices=["x"],
            clarify_id="cid5",
            session_key="sk5",
            binding=entry.binding,
        )
        kwargs = adapter._bot.send_message.call_args[1]
        # Must NOT contain raw <script> — html.escape should have neutralized
        assert "<script>" not in kwargs["text"]
        assert "&lt;script&gt;" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_multi_choice_refuses_missing_binding(self):
        adapter = _make_adapter()
        result = await adapter.send_clarify(
            chat_id="12345",
            question="Pick",
            choices=["x"],
            clarify_id="unbound",
            session_key="sk",
        )

        assert result.success is False
        assert "binding" in result.error.lower()
        adapter._bot.send_message.assert_not_awaited()


# ===========================================================================
# Callback dispatch — _handle_callback_query routing for cl:* prefixes
# ===========================================================================

class TestTelegramClarifyCallback:
    """Verify clicking a button resolves the clarify primitive."""

    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_numeric_choice_resolves_with_choice_text(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        entry = _register_bound()

        query = AsyncMock()
        query.data = "cl:cidA:1"  # green
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.message.message_thread_id = None
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "777"
        query.from_user.first_name = "Tester"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        # Bound callback consumption removes the active lookup atomically but
        # preserves the waiter-owned entry and chosen response.
        assert cm.get_entry("cidA") is None
        assert entry.response == "green"
        assert entry.event.is_set()
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_unbound_callback_fails_closed(self):
        """Telegram must never resolve an unbound pre-binding prompt."""
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        entry = cm.register("legacy", "sk-cb", "Pick", ["red", "green"])
        # Prove that even an old in-process adapter map cannot reopen the
        # removed compatibility path.
        adapter._clarify_state = {"legacy": "sk-cb"}
        query = AsyncMock()
        query.data = "cl:legacy:1"
        query.message = MagicMock(chat_id=12345, message_thread_id=None, text="Pick")
        query.message.chat.type = "private"
        query.from_user = MagicMock(id="777", first_name="Tester")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(
                MagicMock(callback_query=query), MagicMock()
            )

        assert cm.get_entry("legacy") is entry
        assert not entry.event.is_set()
        assert "resolved" in query.answer.call_args.kwargs["text"].lower()
        query.edit_message_text.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("user_id", "chat_id", "thread_id"),
        [
            ("778", 12345, None),
            ("777", 54321, None),
            ("777", 12345, 10),
        ],
    )
    async def test_bound_callback_rejects_each_authenticated_origin_mismatch(
        self, user_id, chat_id, thread_id,
    ):
        """Global allowlisting cannot bypass the prompt's observed origin."""
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        entry = _register_bound(thread_id="9" if thread_id is not None else None)
        query = AsyncMock()
        query.data = f"cl:{entry.clarify_id}:0"
        query.message = MagicMock()
        query.message.chat_id = chat_id
        query.message.chat.type = "private"
        query.message.message_thread_id = thread_id
        query.message.text = "Pick"
        query.from_user = MagicMock(id=user_id, first_name="Tester")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

        assert cm.get_entry(entry.clarify_id) is entry
        assert not entry.event.is_set()
        assert "expired" in query.answer.call_args.kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_bound_callback_rejects_stale_session_then_consumes_once(self):
        from tools import clarify_gateway as cm

        active = {"session_id": "session-1"}
        adapter = _make_adapter()
        entry = _register_bound(active=active)
        query = AsyncMock()
        query.data = f"cl:{entry.clarify_id}:0"
        query.message = MagicMock(chat_id=12345, message_thread_id=None, text="Pick")
        query.message.chat.type = "private"
        query.from_user = MagicMock(id="777", first_name="Tester")

        active["session_id"] = "session-2"
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
        assert not entry.event.is_set()

        active["session_id"] = "session-1"
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

        assert entry.response == "red"
        assert entry.event.is_set()
        assert cm.get_entry(entry.clarify_id) is None
        assert query.answer.call_count == 3

    @pytest.mark.asyncio
    async def test_bound_other_callback_arms_text_once_without_adapter_state(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        entry = _register_bound()
        query = AsyncMock()
        query.data = f"cl:{entry.clarify_id}:other"
        query.message = MagicMock(chat_id=12345, message_thread_id=None, text="Pick")
        query.message.chat.type = "private"
        query.from_user = MagicMock(id="777", first_name="Tester")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())
            await adapter._handle_callback_query(MagicMock(callback_query=query), MagicMock())

        assert entry.awaiting_text is True
        assert entry.callback_consumed is True
        assert not entry.event.is_set()
        assert query.answer.call_count == 2
        assert "expired" in query.answer.call_args.kwargs["text"].lower()


    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        entry = _register_bound(
            clarify_id="cidC", user_id="999", choices=["a", "b"]
        )

        # Hook up a runner that says NOT authorized
        class _DenyRunner:
            async def _handle_message(self, event):
                return None
            def _is_user_authorized(self, source):
                return False

        adapter._message_handler = _DenyRunner()._handle_message

        query = AsyncMock()
        query.data = "cl:cidC:0"
        query.message = MagicMock()
        query.message.chat_id = 12345
        query.message.chat.type = "private"
        query.message.text = "Pick"
        query.from_user = MagicMock()
        query.from_user.id = "999"
        query.from_user.first_name = "Mallory"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await adapter._handle_callback_query(update, context)

        # Must not resolve, must answer with not-authorized message
        assert cm.get_entry("cidC") is entry
        assert not entry.event.is_set()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()


# ===========================================================================
# Base adapter fallback render — text numbered list
# ===========================================================================

class TestBaseAdapterClarifyFallback:
    """Adapters without button overrides should render numbered text."""

    @pytest.mark.asyncio
    async def test_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        # Subclass just enough to instantiate
        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                # Skip base __init__ — we're not exercising it
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append({"chat_id": chat_id, "content": content})
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()

        result = await adapter.send_clarify(
            chat_id="c",
            question="Pick a fruit",
            choices=["apple", "banana"],
            clarify_id="x",
            session_key="s",
        )
        assert result.success is True
        assert len(adapter.sent) == 1
        text = adapter.sent[0]["content"]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text


def test_shared_send_clarify_contract_accepts_binding_across_adapters():
    """Every shared consumer accepts the runner's immutable binding keyword."""
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
    from gateway.relay.adapter import RelayAdapter
    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.google_chat.adapter import GoogleChatAdapter
    from plugins.platforms.photon.adapter import PhotonAdapter
    from plugins.platforms.slack.adapter import SlackAdapter
    from plugins.platforms.whatsapp.adapter import WhatsAppAdapter

    adapters = (
        BasePlatformAdapter,
        TelegramAdapter,
        DiscordAdapter,
        SlackAdapter,
        WhatsAppAdapter,
        WhatsAppCloudAdapter,
        GoogleChatAdapter,
        PhotonAdapter,
        RelayAdapter,
    )
    for adapter_type in adapters:
        parameter = inspect.signature(adapter_type.send_clarify).parameters.get("binding")
        assert parameter is not None, adapter_type.__name__
        assert parameter.default is None, adapter_type.__name__
