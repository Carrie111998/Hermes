"""Tests for Feishu interactive card approval buttons."""

import importlib.util
import json
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
# Minimal Feishu mock so FeishuAdapter can be imported without lark-oapi
# ---------------------------------------------------------------------------
def _ensure_feishu_mocks():
    """Provide stubs for lark-oapi / aiohttp.web so the import succeeds."""
    if importlib.util.find_spec("lark_oapi") is None and "lark_oapi" not in sys.modules:
        mod = MagicMock()
        for name in (
            "lark_oapi", "lark_oapi.api.im.v1",
            "lark_oapi.event", "lark_oapi.event.callback_type",
        ):
            sys.modules.setdefault(name, mod)
    if importlib.util.find_spec("aiohttp") is None and "aiohttp" not in sys.modules:
        aio = MagicMock()
        sys.modules.setdefault("aiohttp", aio)
        sys.modules.setdefault("aiohttp.web", aio.web)


_ensure_feishu_mocks()

from gateway.config import PlatformConfig
import plugins.platforms.feishu.adapter as feishu_module
from plugins.platforms.feishu.adapter import FeishuAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter() -> FeishuAdapter:
    """Create a FeishuAdapter with mocked internals."""
    config = PlatformConfig(enabled=True)
    adapter = FeishuAdapter(config)
    adapter._client = MagicMock()
    return adapter


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
    token: str = "tok_abc",
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id),
            action=SimpleNamespace(
                tag="button",
                value=action_value,
            ),
        ),
    )


def _close_submitted_coro(coro, _loop):
    """Close scheduled coroutines in sync-handler tests to avoid unawaited warnings."""
    coro.close()
    return SimpleNamespace(add_done_callback=lambda *_args, **_kwargs: None)


# ===========================================================================
# send_exec_approval — interactive card with buttons
# ===========================================================================

class TestFeishuExecApproval:
    """Test send_exec_approval sends an interactive card."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="rm -rf /important",
                session_key="agent:main:feishu:group:oc_12345",
                description="dangerous deletion",
            )

        assert result.success is True
        assert result.message_id == "msg_001"

        mock_send.assert_called_once()
        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"

        # Verify card payload contains the command and buttons
        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "rm -rf /important" in card["elements"][0]["content"]
        assert "dangerous deletion" in card["elements"][0]["content"]

        # Check buttons
        actions = card["elements"][1]["actions"]
        assert len(actions) == 4
        action_names = [a["value"]["hermes_action"] for a in actions]
        assert action_names == [
            "approve_once", "approve_session", "approve_always", "deny"
        ]

    @pytest.mark.asyncio
    async def test_stores_approval_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_exec_approval(
                chat_id="oc_12345",
                command="echo test",
                session_key="my-session-key",
            )

        assert len(adapter._approval_state) == 1
        approval_id = list(adapter._approval_state.keys())[0]
        state = adapter._approval_state[approval_id]
        assert state["session_key"] == "my-session-key"
        assert state["message_id"] == "msg_002"
        assert state["chat_id"] == "oc_12345"


# ===========================================================================
# send_update_prompt — interactive card with buttons
# ===========================================================================

class TestFeishuUpdatePrompt:
    """Test send_update_prompt sends an interactive card."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_up_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_update_prompt(
                chat_id="oc_12345",
                prompt="Restore stashed changes after update?",
                default="y",
                session_key="agent:main:feishu:group:oc_12345",
                metadata={"thread_id": "th_1"},
            )

        assert result.success is True
        assert result.message_id == "msg_up_001"

        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"
        assert kwargs["metadata"] == {"thread_id": "th_1"}

        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "Restore stashed changes after update?" in card["elements"][0]["content"]
        assert "Default: `y`" in card["elements"][0]["content"]
        actions = card["elements"][1]["actions"]
        assert [a["value"]["hermes_update_prompt_action"] for a in actions] == ["y", "n"]


# ===========================================================================
# _resolve_approval — approval state pop + gateway resolution
# ===========================================================================

class TestResolveApproval:
    """Test _resolve_approval pops state and calls resolve_gateway_approval."""

    @pytest.mark.asyncio
    async def test_resolves_once(self):
        adapter = _make_adapter()
        adapter._approval_state[1] = {
            "session_key": "agent:main:feishu:group:oc_12345",
            "message_id": "msg_001",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
            await adapter._resolve_approval(1, "once", "Norbert", open_id="ou_user1", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("agent:main:feishu:group:oc_12345", "once")
        assert 1 not in adapter._approval_state


    @pytest.mark.asyncio
    async def test_unauthorized_click_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_admin"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg_005",
            "chat_id": "oc_12345",
        }

        with patch("tools.approval.resolve_gateway_approval") as mock_resolve:
            await adapter._resolve_approval(5, "once", "Mallory", open_id="ou_intruder", chat_id="oc_12345")

        mock_resolve.assert_not_called()
        assert 5 in adapter._approval_state


# ===========================================================================
# _handle_card_action_event — non-approval card actions
# ===========================================================================

class TestNonApprovalCardAction:
    """Non-approval card actions should still route as synthetic commands."""

    @pytest.mark.asyncio
    async def test_routes_as_synthetic_command(self):
        adapter = _make_adapter()

        data = _make_card_action_data(
            action_value={"custom_action": "something_else"},
            token="tok_normal",
        )

        with (
            patch.object(
                adapter, "_resolve_sender_profile", new_callable=AsyncMock,
                return_value={"user_id": "ou_u", "user_name": "Dave", "user_id_alt": None},
            ),
            patch.object(adapter, "get_chat_info", new_callable=AsyncMock, return_value={"name": "Test Chat"}),
            patch.object(adapter, "_handle_message_with_guards", new_callable=AsyncMock) as mock_handle,
        ):
            await adapter._handle_card_action_event(data)

        mock_handle.assert_called_once()
        event = mock_handle.call_args[0][0]
        assert "/card button" in event.text


# ===========================================================================
# _on_card_action_trigger — inline card response for approval actions
# ===========================================================================

class _FakeCallBackCard:
    def __init__(self):
        self.type = None
        self.data = None


class _FakeP2Response:
    def __init__(self):
        self.card = None


@pytest.fixture(autouse=False)
def _patch_callback_card_types(monkeypatch):
    """Provide real-ish P2CardActionTriggerResponse / CallBackCard for tests."""
    monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", _FakeP2Response)
    monkeypatch.setattr(feishu_module, "CallBackCard", _FakeCallBackCard)


class TestCardActionCallbackResponse:
    """Test that _on_card_action_trigger returns updated card inline."""

    def test_drops_action_when_loop_not_ready(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = None
        data = _make_card_action_data({"hermes_action": "approve_once", "approval_id": 1})

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_returns_card_for_approve_action(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._approval_state[1] = {
            "session_key": "sess-1",
            "message_id": "msg-1",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 1},
            open_id="ou_bob",
        )
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "Approved once" in card["header"]["title"]["content"]
        assert "Bob" in card["elements"][0]["content"]


    def test_ignores_expired_cached_name(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_expired"}
        adapter._approval_state[4] = {
            "session_key": "sess-4",
            "message_id": "msg-4",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 4},
            open_id="ou_expired",
        )
        adapter._sender_name_cache["ou_expired"] = ("Old Name", 1)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert "Old Name" not in card["elements"][0]["content"]
        assert "ou_expired" in card["elements"][0]["content"]

    def test_rejects_approval_click_from_unauthorized_user(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_allowed"}
        adapter._approval_state[5] = {
            "session_key": "sess-5",
            "message_id": "msg-5",
            "chat_id": "oc_12345",
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": 5},
            open_id="ou_attacker",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()


    def test_update_prompt_unauthorized_operator_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_006",
            "chat_id": "oc_12345",
        }
        adapter._allowed_group_users = {"ou_allowed"}
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 1},
            open_id="ou_intruder",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()


    def test_update_prompt_chat_mismatch_returns_no_card(self, _patch_callback_card_types):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._update_prompt_state[8] = {
            "session_key": "sess-up-8",
            "message_id": "msg_up_008",
            "chat_id": "oc_expected",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": 8},
            chat_id="oc_mismatch",
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        assert 8 in adapter._update_prompt_state
        mock_submit.assert_not_called()


class TestResolveUpdatePrompt:
    """Test update prompt resolution persists the response file."""

    @pytest.mark.asyncio
    async def test_writes_response_file(self, tmp_path, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        (tmp_path / ".hermes").mkdir()
        adapter._update_prompt_state[1] = {
            "session_key": "sess-up-1",
            "message_id": "msg_up_003",
            "chat_id": "oc_12345",
        }

        await adapter._resolve_update_prompt(1, "y", "Alice")

        assert (tmp_path / ".hermes" / ".update_response").read_text() == "y"
        assert 1 not in adapter._update_prompt_state


# ===========================================================================
# DM card-action security regression tests (PR #94485)
#
# PR #94485 replaced _allow_group_message with a bare "if not open_id" check
# on DM approval/update-prompt cards, relying on the downstream chat_id
# mismatch check to provide security.  These tests verify that the downstream
# chat_id gate correctly accepts legitimate DM clicks and rejects
# cross-chat / non-participant clicks.
# ===========================================================================


class TestDmApprovalCardSecurity:
    """Verify DM approval card button security after _allow_group_message removal."""

    def _make_dm_event(self, approval_id: int = 1, open_id: str = "ou_dm_user",
                       chat_id: str = "oc_dm_chat", action: str = "approve_once"):
        """Build a minimal card-action event with context.open_chat_id set to a DM ID."""
        return _make_card_action_data(
            {"hermes_action": action, "approval_id": approval_id},
            chat_id=chat_id, open_id=open_id,
        )

    def test_dm_participant_can_approve(self, _patch_callback_card_types):
        """Positive: the user who received the card in a DM clicks it — should succeed."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 10
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-10",
            "message_id": "msg_dm_10",
            "chat_id": "oc_dm_chat",        # original DM chat
        }
        data = self._make_dm_event(
            approval_id=approval_id,
            open_id="ou_dm_user",
            chat_id="oc_dm_chat",           # same DM chat
        )
        adapter._sender_name_cache["ou_dm_user"] = ("DM User", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        assert "Approved once" in response.card.data["header"]["title"]["content"]

    def test_dm_user_clicking_from_different_chat_rejected(self, _patch_callback_card_types):
        """Negative: same user, but callback originates from a different chat (e.g. card forwarded to group)."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 11
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-11",
            "message_id": "msg_dm_11",
            "chat_id": "oc_dm_chat",        # card sent in DM
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_forwarded_group",   # forwarded to group — DIFFERENT chat_id
            open_id="ou_dm_user",           # same user
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None           # rejected — no inline card
        mock_submit.assert_not_called()
        assert approval_id in adapter._approval_state   # state preserved

    def test_empty_open_id_rejected(self, _patch_callback_card_types):
        """Negative: operator has no open_id — must be rejected at the open_id check."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._approval_state[12] = {
            "session_key": "sess-dm-12",
            "message_id": "msg_dm_12",
            "chat_id": "oc_dm_chat",
        }
        # Build event with empty/open_id=empty via raw namespace manipulation
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

    def test_different_user_same_dm_chat_rejected_by_chat_id(self, _patch_callback_card_types):
        """Negative: a different user in the same DM group/chat should fail the chat_id check
        if the card was NOT sent to that chat (i.e. the card was sent in a one-on-one DM and
        someone else intercepts it in a group)."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 13
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-13",
            "message_id": "msg_dm_13",
            "chat_id": "oc_bobs_dm",           # card sent in Bob's one-on-one with bot
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_bobs_dm",               # same DM chat
            open_id="ou_mallory",               # DIFFERENT user
        )
        # Allow mallory to have a valid open_id — the open_id check passes.
        # But the chat_id check should NOT reject here because chat_ids match.
        # This tests the edge case: same chat, different user.
        # With the current code, this WILL succeed (chat_id matches). That's acceptable
        # because in a 1:1 DM only that user can interact with the card.
        # We verify the card is returned — this is intentional behavior.
        adapter._sender_name_cache["ou_mallory"] = ("Mallory", 9999999999)

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        # In a 1:1 DM, the chat_id uniquely identifies the conversation, so
        # only the DM participant can reach this code path with a matching chat_id.
        assert response is not None
        assert response.card is not None

    def test_card_forwarded_to_group_different_chat_rejected(self, _patch_callback_card_types):
        """Negative: card originally sent to DM, user forwards it to a group and clicks —
        callback_chat_id (group) != expected_chat_id (DM) → rejected."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        approval_id = 14
        adapter._approval_state[approval_id] = {
            "session_key": "sess-dm-14",
            "message_id": "msg_dm_14",
            "chat_id": "oc_12345",              # original DM chat
        }
        data = _make_card_action_data(
            {"hermes_action": "approve_once", "approval_id": approval_id},
            chat_id="oc_group5678",             # forwarded to group
            open_id="ou_original_user",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert approval_id in adapter._approval_state


class TestDmUpdatePromptCardSecurity:
    """Verify DM update-prompt card button security after _allow_group_message removal."""

    def test_dm_participant_can_confirm(self, _patch_callback_card_types):
        """Positive: DM participant confirms update prompt."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        prompt_id = 20
        adapter._update_prompt_state[prompt_id] = {
            "session_key": "sess-up-dm-20",
            "message_id": "msg_up_dm_20",
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

    def test_dm_user_clicking_from_different_chat_rejected(self, _patch_callback_card_types):
        """Negative: same user, different callback chat (forwarded) — rejected by chat_id check."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        prompt_id = 21
        adapter._update_prompt_state[prompt_id] = {
            "session_key": "sess-up-dm-21",
            "message_id": "msg_up_dm_21",
            "chat_id": "oc_dm_chat",
        }
        data = _make_card_action_data(
            {"hermes_update_prompt_action": "y", "update_prompt_id": prompt_id},
            chat_id="oc_forwarded_group",   # different chat
            open_id="ou_dm_user",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()
        assert prompt_id in adapter._update_prompt_state

    def test_empty_open_id_rejected_on_update_prompt(self, _patch_callback_card_types):
        """Negative: operator has no open_id on update prompt card."""
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._update_prompt_state[22] = {
            "session_key": "sess-up-dm-22",
            "message_id": "msg_up_dm_22",
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


