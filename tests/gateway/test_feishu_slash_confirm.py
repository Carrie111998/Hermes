"""Tests for Feishu interactive card slash-command confirmation buttons."""

import importlib.util
import json
import sys
import time
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


def _make_state(**overrides) -> dict:
    """Build a fresh (non-expired) slash-confirm state entry."""
    state = {
        "session_key": "sess-1",
        "confirm_id": "gw-1",
        "message_id": "msg-1",
        "chat_id": "oc_12345",
        "created_at": time.time(),
    }
    state.update(overrides)
    return state


def _make_card_action_data(
    action_value: dict,
    chat_id: str = "oc_12345",
    open_id: str = "ou_user1",
) -> SimpleNamespace:
    """Create a mock Feishu card action callback data object."""
    return SimpleNamespace(
        event=SimpleNamespace(
            token="tok_abc",
            context=SimpleNamespace(open_chat_id=chat_id),
            operator=SimpleNamespace(open_id=open_id, user_id=open_id),
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


class _FakeCallBackCard:
    def __init__(self):
        self.type = None
        self.data = None


class _FakeP2Response:
    def __init__(self):
        self.card = None


@pytest.fixture(autouse=True)
def _patch_callback_card_types(monkeypatch):
    """Provide real-ish P2CardActionTriggerResponse / CallBackCard for tests."""
    monkeypatch.setattr(feishu_module, "P2CardActionTriggerResponse", _FakeP2Response)
    monkeypatch.setattr(feishu_module, "CallBackCard", _FakeCallBackCard)


# ===========================================================================
# send_slash_confirm — interactive card with three buttons
# ===========================================================================

class TestFeishuSendSlashConfirm:
    """Test send_slash_confirm sends an interactive card."""

    @pytest.mark.asyncio
    async def test_sends_interactive_card(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_sc_001"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_send:
            result = await adapter.send_slash_confirm(
                chat_id="oc_12345",
                title="Confirm /reload-mcp",
                message="This invalidates the provider prompt cache.",
                session_key="agent:main:feishu:dm:oc_12345",
                confirm_id="gw-42",
            )

        assert result.success is True
        assert result.message_id == "msg_sc_001"

        kwargs = mock_send.call_args[1]
        assert kwargs["chat_id"] == "oc_12345"
        assert kwargs["msg_type"] == "interactive"

        card = json.loads(kwargs["payload"])
        assert card["header"]["template"] == "orange"
        assert "This invalidates the provider prompt cache." in card["elements"][0]["content"]

        actions = card["elements"][1]["actions"]
        assert len(actions) == 3
        choices = [a["value"]["hermes_slash_confirm_action"] for a in actions]
        assert choices == ["once", "always", "cancel"]
        # The button value carries the prompt id under an unambiguous key.
        assert all(a["value"]["prompt_id"] for a in actions)

    @pytest.mark.asyncio
    async def test_stores_confirm_state(self):
        adapter = _make_adapter()

        mock_response = SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="msg_sc_002"),
        )
        with patch.object(
            adapter, "_feishu_send_with_retry", new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await adapter.send_slash_confirm(
                chat_id="oc_12345",
                title="t",
                message="m",
                session_key="my-session-key",
                confirm_id="gw-7",
            )

        assert len(adapter._slash_confirm_state) == 1
        prompt_id = list(adapter._slash_confirm_state.keys())[0]
        state = adapter._slash_confirm_state[prompt_id]
        assert state["session_key"] == "my-session-key"
        assert state["confirm_id"] == "gw-7"
        assert state["message_id"] == "msg_sc_002"
        assert state["chat_id"] == "oc_12345"
        assert "created_at" in state


# ===========================================================================
# _handle_slash_confirm_card_action — card callback routing
# ===========================================================================

class TestHandleSlashConfirmCardAction:
    """Test card button clicks route to tools.slash_confirm.resolve."""

    def test_returns_resolved_card_for_once(self):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._slash_confirm_state["1"] = _make_state()
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)
        data = _make_card_action_data(
            {"hermes_slash_confirm_action": "once", "prompt_id": "1"},
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is not None
        assert response.card.type == "raw"
        card = response.card.data
        assert card["header"]["template"] == "green"
        assert "Approved once" in card["header"]["title"]["content"]

    def test_returns_resolved_card_for_cancel(self):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._slash_confirm_state["2"] = _make_state()
        adapter._sender_name_cache["ou_bob"] = ("Bob", 9999999999)
        data = _make_card_action_data(
            {"hermes_slash_confirm_action": "cancel", "prompt_id": "2"},
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe", side_effect=_close_submitted_coro):
            response = adapter._on_card_action_trigger(data)

        card = response.card.data
        assert card["header"]["template"] == "red"
        assert "Cancelled" in card["header"]["title"]["content"]

    def test_rejects_click_from_unauthorized_user(self):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_allowed"}
        adapter._slash_confirm_state["3"] = _make_state()
        data = _make_card_action_data(
            {"hermes_slash_confirm_action": "once", "prompt_id": "3"},
            open_id="ou_attacker",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response is not None
        assert response.card is None
        mock_submit.assert_not_called()

    def test_chat_mismatch_returns_no_card(self):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._slash_confirm_state["4"] = _make_state(chat_id="oc_expected")
        data = _make_card_action_data(
            {"hermes_slash_confirm_action": "once", "prompt_id": "4"},
            chat_id="oc_mismatch",
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response.card is None
        mock_submit.assert_not_called()
        assert "4" in adapter._slash_confirm_state

    def test_expired_confirm_is_dropped(self):
        adapter = _make_adapter()
        adapter._loop = MagicMock()
        adapter._loop.is_closed = MagicMock(return_value=False)
        adapter._allowed_group_users = {"ou_bob"}
        adapter._slash_confirm_state["9"] = _make_state(created_at=time.time() - 1000)
        data = _make_card_action_data(
            {"hermes_slash_confirm_action": "once", "prompt_id": "9"},
            open_id="ou_bob",
        )

        with patch("asyncio.run_coroutine_threadsafe") as mock_submit:
            response = adapter._on_card_action_trigger(data)

        assert response.card is None
        mock_submit.assert_not_called()
        assert "9" not in adapter._slash_confirm_state


# ===========================================================================
# _resolve_slash_confirm — async resolution via tools.slash_confirm
# ===========================================================================

class TestResolveSlashConfirm:
    """Test _resolve_slash_confirm pops state and calls tools.slash_confirm.resolve."""

    @pytest.mark.asyncio
    async def test_resolves_once(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_bob"}
        adapter._slash_confirm_state["1"] = _make_state()

        with patch("tools.slash_confirm.resolve", new_callable=AsyncMock, return_value="ok") as mock_resolve:
            await adapter._resolve_slash_confirm("1", "once", "Bob", open_id="ou_bob", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("sess-1", "gw-1", "once")
        assert "1" not in adapter._slash_confirm_state

    @pytest.mark.asyncio
    async def test_unauthorized_click_does_not_resolve(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_admin"}
        adapter._slash_confirm_state["5"] = _make_state()

        with patch("tools.slash_confirm.resolve", new_callable=AsyncMock) as mock_resolve:
            await adapter._resolve_slash_confirm("5", "once", "Mallory", open_id="ou_intruder", chat_id="oc_12345")

        mock_resolve.assert_not_called()
        # State is claimed atomically at coroutine entry, so the unauthorized
        # click still consumes it even though the resolver refuses to run.
        assert "5" not in adapter._slash_confirm_state

    @pytest.mark.asyncio
    async def test_double_resolve_runs_once(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_bob"}
        adapter._slash_confirm_state["1"] = _make_state()

        with patch("tools.slash_confirm.resolve", new_callable=AsyncMock, return_value="ok") as mock_resolve:
            await adapter._resolve_slash_confirm("1", "once", "Bob", open_id="ou_bob", chat_id="oc_12345")
            await adapter._resolve_slash_confirm("1", "once", "Bob", open_id="ou_bob", chat_id="oc_12345")

        mock_resolve.assert_called_once_with("sess-1", "gw-1", "once")

    @pytest.mark.asyncio
    async def test_resolve_none_sends_expiry_notice(self):
        adapter = _make_adapter()
        adapter._admins = {"ou_bob"}
        adapter._slash_confirm_state["6"] = _make_state()

        with patch("tools.slash_confirm.resolve", new_callable=AsyncMock, return_value=None) as mock_resolve, \
             patch.object(adapter, "send", new_callable=AsyncMock) as mock_send:
            await adapter._resolve_slash_confirm("6", "once", "Bob", open_id="ou_bob", chat_id="oc_12345")

        mock_resolve.assert_called_once()
        mock_send.assert_awaited_once()
        notice = mock_send.call_args[0][1]
        assert "expired" in notice
