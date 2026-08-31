"""DM card-action security regression tests (complementary to PR #99004).

These tests validate the original #94485 fix: after removing _allow_group_message()
from DM approval/update-prompt cards, the downstream chat_id gate correctly accepts
legitimate DM clicks and rejects cross-chat / non-participant clicks.
"""

import importlib.util, json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

def _ensure_feishu_mocks():
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in ("lark_oapi", "lark_oapi.api.im.v1", "lark_oapi.event", "lark_oapi.event.callback_type"):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)

_ensure_feishu_mocks()

from gateway.config import PlatformConfig
import plugins.platforms.feishu.adapter as feishu_module
from plugins.platforms.feishu.adapter import FeishuAdapter

def _make_adapter() -> FeishuAdapter:
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter

def _make_card_action_data(action_value, chat_id="oc_12345", open_id="ou_user1", token="tok_abc"):
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(tag="button", value=action_value),
        ),
    )

def _close_submitted_coro(coro, _loop):
    coro.close()
    return SimpleNamespace(add_done_callback=lambda *_args, **_kwargs: None)

# Helper for DM-scenario tests that use context.open_chat_id
def _make_dm_event(approval_id=1, open_id="ou_dm_user", chat_id="oc_dm_chat"):
    return _make_card_action_data(
        {"hermes_action": "approve_once", "approval_id": approval_id},
        chat_id=chat_id, open_id=open_id,
    )


class TestDmApprovalCardSecurity:
    """DM approval card button security after _allow_group_message removal."""

    def test_dm_participant_can_approve(self, _patch_callback_card_types=None):
        """Positive: DM user who received the card clicks it."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 10
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-10",
            "message_id": "msg_dm-10",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_dm_chat", open_id="ou_dm_user",
        )
        adapter._sender_name_cache["ou_dm_user"] = ("DM User", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        assert "Approved once" in response.card.data["header"]["title"]["content"]

    def test_dm_user_clicking_from_different_chat_rejected(self, _patch_callback_card_types=None):
        """Negative: same user, callback from different chat (e.g. card forwarded)."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 11
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-11",
            "message_id": "msg_dm-11",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_forwarded_group",
            open_id="ou_dm_user",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert approval_id in adapter._approval_state

    def test_empty_open_id_rejected(self, _patch_callback_card_types=None):
        """Negative: operator has no open_id."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._approval_state[12] = {
            "session_key": "sess-dm-12",
            "message_id": "msg_dm-12",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 12},
            open_id="",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert 12 in adapter._approval_state

    def test_card_forwarded_to_group_different_chat_rejected(self, _patch_callback_card_types=None):
        """Negative: DM card forwarded to group and clicked by original user."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 14
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-14",
            "message_id": "msg_dm-14",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_group5678",
            open_id="ou_original_user",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert approval_id in adapter._approval_state


class TestDmUpdatePromptCardSecurity:
    """DM update-prompt card button security after _allow_group_message removal."""

    def test_dm_participant_can_confirm(self, _patch_callback_card_types=None):
        """Positive: DM participant confirms update prompt."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        prompt_id = 20
        adapter._update_prompt_state[prompt_id] = {
            "session_key": "sess-up-dm-20",
            "message_id": "msg_up_dm-20",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": prompt_id},
            chat_id="oc_dm_chat", open_id="ou_dm_user",
        )
        adapter._sender_name_cache["ou_dm_user"] = ("DM User", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None

    def test_dm_user_clicking_from_different_chat_rejected(self, _patch_callback_card_types=None):
        """Negative: same user, different callback chat (forwarded)."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        prompt_id = 21
        adapter._update_prompt_state[prompt_id] = {
            "session_key": "sess-up-dm-21",
            "message_id": "msg_up_dm-21",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": prompt_id},
            chat_id="oc_forwarded_group",
            open_id="ou_dm_user",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert prompt_id in adapter._update_prompt_state

    def test_empty_open_id_rejected_on_update_prompt(self, _patch_callback_card_types=None):
        """Negative: operator has no open_id on update prompt card."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[22] = {
            "session_key": "sess-up-dm-22",
            "message_id": "msg_up_dm-22",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 22},
            open_id="",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert 22 in adapter._update_prompt_state
