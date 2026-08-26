"""The real authorization boundary for Telegram action-button taps (#15311).

A tap is admitted by the SAME chain a typed message goes through — adapter
``allow_from`` / ``group_allow_from`` as a hard constraint first, then the
profile-bound callback registered via ``set_authorization_check()`` — and that
decision happens BEFORE the nonce is consumed.

Nothing here stubs the adapter's auth helpers or installs a fake handler: the
tests drive ``_handle_callback_query`` end to end so the boundary itself is
under test. Companion coverage for routing/nonce lifetime lives in
``test_telegram_action_buttons.py``.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from gateway.config import GatewayConfig, Platform, PlatformConfig  # noqa: E402
from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


def _make_adapter(extra=None):
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    )
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


def _tg_message(*, chat_id, message_id=100, chat_type="private"):
    """The message a keyboard is delivered on — and that a tap comes back from."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.chat = MagicMock()
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.chat.is_forum = False
    msg.message_thread_id = None
    msg.is_topic_message = False
    return msg


async def _send_button(adapter, *, chat_id, chat_type="private"):
    """Deliver a one-button keyboard; returns (callback_data, message)."""
    message = _tg_message(chat_id=int(chat_id), chat_type=chat_type)
    adapter._bot.send_message = AsyncMock(return_value=message)
    result = await adapter.send(
        str(chat_id), "Deploy?", metadata={"buttons": [{"text": "Ship it", "value": "ship"}]},
    )
    assert result.success is True
    nonce = next(iter(adapter._action_button_state))
    return f"hb1:{nonce}", message


def _tap_update(callback_data, message, user_id):
    query = AsyncMock()
    query.data = callback_data
    query.message = message
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.username = "tester"
    query.from_user.first_name = "Tester"
    query.answer = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def _observe(adapter):
    seen = []

    async def handler(event, source):
        seen.append((event, source))

    adapter.set_platform_event_handler(handler)
    return seen


def _refused(query) -> bool:
    return "not authorized" in query.answer.await_args[1]["text"].lower()


# ===========================================================================
# Adapter allow_from / group_allow_from are hard constraints
# ===========================================================================

class TestAdapterAllowlistBoundary:
    @pytest.mark.asyncio
    async def test_group_allow_from_refuses_a_globally_allowed_user(self):
        """In TELEGRAM_ALLOWED_USERS but not in group_allow_from → no tap.

        Such a user cannot post in the group at all
        (``_is_user_authorized_from_message`` denies them), so tapping a button
        there must not be a way around that.
        """
        adapter = _make_adapter({"group_allow_from": ["555"]})
        callback_data, message = await _send_button(
            adapter, chat_id=-1001, chat_type="supergroup",
        )
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "777")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777,555"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert seen == []
        assert _refused(query)
        # Refused before the pop: the button still works for its own group.
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_group_allow_from_admits_its_own_member(self):
        adapter = _make_adapter({"group_allow_from": ["555"]})
        callback_data, message = await _send_button(
            adapter, chat_id=-1001, chat_type="supergroup",
        )
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "555")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert len(seen) == 1
        assert seen[0][1].user_id == "555"
        query.answer.assert_awaited_once_with(text="Ship it")
        assert adapter._action_button_state == {}

    @pytest.mark.asyncio
    async def test_dm_allow_from_does_not_leak_into_groups(self):
        """DMs read allow_from, groups read group_allow_from — same as messages."""
        adapter = _make_adapter({"allow_from": ["777"], "group_allow_from": ["555"]})
        callback_data, message = await _send_button(
            adapter, chat_id=-1001, chat_type="supergroup",
        )
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "777")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert seen == []
        assert _refused(query)

    @pytest.mark.asyncio
    async def test_no_allowlist_and_no_runner_denies(self):
        """Fail-closed (#24457): an undecidable tap is not an admitted tap."""
        adapter = _make_adapter()
        callback_data, message = await _send_button(adapter, chat_id=12345)
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "777")

        with patch.dict(
            os.environ,
            {"TELEGRAM_ALLOWED_USERS": "", "GATEWAY_ALLOW_ALL_USERS": ""},
            clear=False,
        ):
            await adapter._handle_callback_query(update, MagicMock())

        assert seen == []
        assert _refused(query)
        assert len(adapter._action_button_state) == 1


# ===========================================================================
# The profile-bound callback decides under multiplexing
# ===========================================================================

class TestProfileBoundBoundary:
    """``_message_handler`` is a profile CLOSURE under multiplex_profiles, so it
    has no ``__self__`` to reach the runner through (#87132). The registered
    ``set_authorization_check`` callback is the authority; falling back to the
    process-wide env allowlist would authorize the wrong profile's users."""

    @staticmethod
    def _wire(adapter, allowed):
        """Register a profile-bound check and a __self__-less message handler."""
        calls = []

        def check(user_id, chat_type=None, chat_id=None):
            calls.append((user_id, chat_type, chat_id))
            return user_id in allowed

        async def profile_message_handler(event):  # closure: no __self__
            return None

        adapter.set_authorization_check(check)
        adapter.set_message_handler(profile_message_handler)
        return calls

    @pytest.mark.asyncio
    async def test_registered_check_refuses_another_profiles_user(self):
        adapter = _make_adapter()
        calls = self._wire(adapter, {"555"})
        callback_data, message = await _send_button(adapter, chat_id=12345)
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "777")

        # The env allowlist would admit everyone; the profile's own check must win.
        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "*"}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert seen == []
        assert _refused(query)
        assert calls == [("777", "dm", "12345")]
        assert len(adapter._action_button_state) == 1

    @pytest.mark.asyncio
    async def test_registered_check_admits_its_own_profiles_user(self):
        adapter = _make_adapter()
        calls = self._wire(adapter, {"555"})
        callback_data, message = await _send_button(adapter, chat_id=12345)
        seen = _observe(adapter)
        update, query = _tap_update(callback_data, message, "555")

        with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": ""}, clear=False):
            await adapter._handle_callback_query(update, MagicMock())

        assert len(seen) == 1
        assert calls == [("555", "dm", "12345")]
        query.answer.assert_awaited_once_with(text="Ship it")

    @pytest.mark.asyncio
    async def test_real_runner_check_uses_the_profiles_scoped_allowlist(self):
        """End-to-end through GatewayRunner._is_user_authorized under multiplex.

        The profile's secret scope — not the first-writer-wins process env —
        is what the tap is judged against (#72348).
        """
        from agent import secret_scope
        from gateway.run import GatewayRunner

        adapter = _make_adapter()
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(multiplex_profiles=True)
        runner.adapters = {}
        runner._profile_adapters = {"coder": {Platform.TELEGRAM: adapter}}
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False

        seen_sources = []
        real_check = runner._make_adapter_auth_check(
            Platform.TELEGRAM, profile_name="coder",
        )

        def check(user_id, chat_type=None, chat_id=None):
            seen_sources.append((user_id, chat_type, chat_id))
            return real_check(user_id, chat_type, chat_id)

        adapter.set_authorization_check(check)

        async def profile_message_handler(event):  # closure: no __self__
            return None

        adapter.set_message_handler(profile_message_handler)

        callback_data, message = await _send_button(adapter, chat_id=12345)
        seen = _observe(adapter)
        outsider, outsider_query = _tap_update(callback_data, message, "777")
        insider, insider_query = _tap_update(callback_data, message, "555")

        was_multiplex = secret_scope.is_multiplex_active()
        secret_scope.set_multiplex_active(True)
        token = secret_scope.set_secret_scope({"TELEGRAM_ALLOWED_USERS": "555"})
        try:
            # Another profile's bridged value sits in the process env.
            with patch.dict(os.environ, {"TELEGRAM_ALLOWED_USERS": "777"}, clear=False):
                await adapter._handle_callback_query(outsider, MagicMock())
                assert seen == []
                assert _refused(outsider_query)
                assert len(adapter._action_button_state) == 1

                await adapter._handle_callback_query(insider, MagicMock())
        finally:
            secret_scope.reset_secret_scope(token)
            secret_scope.set_multiplex_active(was_multiplex)

        assert len(seen) == 1
        assert seen[0][1].user_id == "555"
        assert seen_sources == [("777", "dm", "12345"), ("555", "dm", "12345")]
        assert adapter._action_button_state == {}
