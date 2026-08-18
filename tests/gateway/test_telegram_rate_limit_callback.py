"""Tests for the Telegram model-rate-limit reroute callback (Task 7).

Mirrors tests/gateway/test_telegram_approval_buttons.py's
TestTelegramApprovalCallback shape for the new ``rl:action:token`` branch
of ``_handle_callback_query``.

The token is deliberately opaque -- it never carries the model name (see
events/override_buttons.py). These tests drive the handler through
events.override_callback_state exactly the way
events/subscribers/telegram_notifier.py populates it when it sends the
buttons, then simulate the tap and assert on the resulting
events.model_override store.
"""

import os
import sys
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
# test_telegram_approval_buttons.py / test_telegram_clarify_buttons.py)
# ---------------------------------------------------------------------------
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

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _make_query(data, user_id="12345", first_name="Norbert"):
    query = AsyncMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat_id = 12345
    query.message.chat.type = "private"
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.first_name = first_name
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def _make_update(query):
    update = MagicMock()
    update.callback_query = query
    return update


@pytest.fixture(autouse=True)
def _isolated_override_state(tmp_path, monkeypatch):
    """Isolate events.model_override's file store and
    events.override_callback_state's in-memory map for every test in this
    module (mirrors tests/events/test_model_override.py's ``ov`` fixture)."""
    store_path = tmp_path / "model_overrides.json"
    monkeypatch.setattr("events.model_override._store_path", lambda: store_path)
    from events import model_override, override_callback_state

    model_override.reset_cache()
    override_callback_state.reset()
    yield
    override_callback_state.reset()
    model_override.reset_cache()


def _record_target(token, provider="deepseek", model="deepseek-v4-pro",
                    replacement_provider="openai-codex", replacement_model="gpt-5.6-sol"):
    from events import override_callback_state
    override_callback_state.record(
        token,
        provider=provider,
        model=model,
        replacement_provider=replacement_provider,
        replacement_model=replacement_model,
    )


class TestRateLimitDivertCallback:
    """rl:divert:token — authorized tap writes the override and retires
    the buttons."""

    @pytest.mark.asyncio
    async def test_authorized_tap_writes_override_and_retires_buttons(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok1")
        query = _make_query("rl:divert:tok1")
        update = _make_update(query)
        context = MagicMock()

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, context)

        rec = get_override("deepseek", "deepseek-v4-pro")
        assert rec is not None, "authorized divert tap must write an override"
        assert rec["replacement_provider"] == "openai-codex"
        assert rec["replacement_model"] == "gpt-5.6-sol"
        assert rec["set_by"] == "telegram:12345"

        query.answer.assert_called_once()
        assert "diverted" in query.answer.call_args[1]["text"].lower()

        query.edit_message_text.assert_called_once()
        edit_kwargs = query.edit_message_text.call_args[1]
        assert edit_kwargs["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_set_by_identifies_the_tapping_user(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-user")
        query = _make_query("rl:divert:tok-user", user_id="98765")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        rec = get_override("deepseek", "deepseek-v4-pro")
        assert rec["set_by"] == "telegram:98765"


class TestRateLimitUnauthorizedCallback:
    """An unauthorized tap must write NOTHING."""

    @pytest.mark.asyncio
    async def test_unauthorized_tap_writes_nothing(self):
        from events.model_override import get_override
        from events import override_callback_state

        adapter = _make_adapter()
        _record_target("tok-unauth")
        query = _make_query("rl:divert:tok-unauth", user_id="222", first_name="Mallory")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "67890"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.edit_message_text.assert_not_called()
        query.answer.assert_called_once()
        assert "not authorized" in query.answer.call_args[1]["text"].lower()

        # The token must still be there for a legitimate follow-up tap —
        # an unauthorized attempt must not consume or corrupt state.
        assert override_callback_state.pop("tok-unauth") is not None


class TestRateLimitUnknownTokenCallback:
    """An unknown/expired token must answer 'already resolved' and write
    nothing."""

    @pytest.mark.asyncio
    async def test_unknown_token_writes_nothing(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        # No _record_target call — token was never issued.
        query = _make_query("rl:divert:ghost-token")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        assert "already been resolved" in query.answer.call_args[1]["text"]
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_double_tap_is_idempotent(self):
        """Second tap on the same token finds nothing and no-ops."""
        from events.model_override import get_override, clear_override

        adapter = _make_adapter()
        _record_target("tok-double")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            query1 = _make_query("rl:divert:tok-double")
            await adapter._handle_callback_query(_make_update(query1), MagicMock())
            assert get_override("deepseek", "deepseek-v4-pro") is not None

            # Simulate the operator un-diverting in between — a second tap
            # on the same (already-consumed) token must NOT re-write it.
            clear_override(provider="deepseek", model="deepseek-v4-pro", cleared_by="test")
            assert get_override("deepseek", "deepseek-v4-pro") is None

            query2 = _make_query("rl:divert:tok-double")
            await adapter._handle_callback_query(_make_update(query2), MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None, (
            "a second tap on an already-consumed token must not write anything"
        )
        query2.answer.assert_called_once()
        assert "already been resolved" in query2.answer.call_args[1]["text"]
        query2.edit_message_text.assert_not_called()


class TestRateLimitRejectedWrite:
    """A refused write (target already limited / self-target) must surface
    the reason, not silently do nothing."""

    @pytest.mark.asyncio
    async def test_self_target_rejection_surfaces_reason(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        # A self-target: replacement is the same as the original — set_override
        # rejects this outright (routing loop).
        _record_target(
            "tok-self", provider="deepseek", model="deepseek-v4-pro",
            replacement_provider="deepseek", replacement_model="deepseek-v4-pro",
        )
        query = _make_query("rl:divert:tok-self")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        toast = query.answer.call_args[1]["text"]
        assert "not diverted" in toast.lower()
        assert "loop" in toast.lower() or "itself" in toast.lower()

        # Buttons are still retired -- the token was consumed either way.
        query.edit_message_text.assert_called_once()
        edit_kwargs = query.edit_message_text.call_args[1]
        assert edit_kwargs["reply_markup"] is None


class TestRateLimitChooseAndDismiss:
    @pytest.mark.asyncio
    async def test_choose_does_not_write_an_override(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-choose")
        query = _make_query("rl:choose:tok-choose")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once()
        assert query.edit_message_text.call_args[1]["reply_markup"] is None

    @pytest.mark.asyncio
    async def test_dismiss_retires_buttons_and_writes_nothing(self):
        from events.model_override import get_override

        adapter = _make_adapter()
        _record_target("tok-dismiss")
        query = _make_query("rl:dismiss:tok-dismiss")
        update = _make_update(query)

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert get_override("deepseek", "deepseek-v4-pro") is None
        query.edit_message_text.assert_called_once()
        assert query.edit_message_text.call_args[1]["reply_markup"] is None
