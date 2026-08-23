from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import BasePlatformAdapter, MessageEvent
from gateway.run import GatewayRunner


class _StubAdapter(BasePlatformAdapter):
    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, **kwargs):
        return None

    async def get_chat_info(self, chat_id):
        return None


def _idle_adapter():
    adapter = object.__new__(_StubAdapter)
    adapter._message_handler = MagicMock()
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._start_session_processing = MagicMock(return_value=True)
    return adapter


def _runner(adapter, source):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._heartbeat_watch = {"session-key": (source, "session-id")}
    runner._running_agents = {}
    runner._warm_goals_session_db = AsyncMock()
    runner._adapter_for_source = MagicMock(return_value=adapter)
    return runner


def test_internal_turn_starts_a_consumer_only_when_session_is_idle():
    adapter = _idle_adapter()
    event = MagicMock()

    assert adapter.start_internal_turn(event, "session-key") is True
    adapter._start_session_processing.assert_called_once_with(event, "session-key")

    adapter._active_sessions["session-key"] = MagicMock()
    assert adapter.start_internal_turn(event, "session-key") is False


def test_internal_turn_preserves_pending_user_input():
    adapter = _idle_adapter()
    adapter._pending_messages["session-key"] = MagicMock()

    assert adapter.start_internal_turn(MagicMock(), "session-key") is False
    adapter._start_session_processing.assert_not_called()


@pytest.mark.asyncio
async def test_due_heartbeat_starts_idle_turn_before_confirming_fire(monkeypatch):
    source = SimpleNamespace(chat_id="chat", thread_id="topic", platform="telegram")
    manager = MagicMock()
    manager.has_heartbeat.return_value = True
    manager.claim_due_prompt.return_value = "heartbeat prompt"
    adapter = MagicMock()
    adapter.start_internal_turn.return_value = True
    runner = _runner(adapter, source)
    monkeypatch.setattr("hermes_cli.heartbeat.HeartbeatManager", lambda session_id: manager)

    await runner._poll_heartbeat_watches_once()

    event, session_key = adapter.start_internal_turn.call_args.args
    assert isinstance(event, MessageEvent)
    assert event.text == "heartbeat prompt"
    assert event.internal is True
    assert event.allow_gateway_control is False
    assert session_key == "session-key"
    manager.confirm_claim.assert_called_once_with()
    manager.abandon_claim.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_heartbeat_turn_is_not_counted(monkeypatch, caplog):
    source = SimpleNamespace(chat_id="chat", thread_id="topic", platform="telegram")
    manager = MagicMock()
    manager.has_heartbeat.return_value = True
    manager.claim_due_prompt.return_value = "heartbeat prompt"
    adapter = MagicMock()
    adapter.start_internal_turn.return_value = False
    runner = _runner(adapter, source)
    monkeypatch.setattr("hermes_cli.heartbeat.HeartbeatManager", lambda session_id: manager)

    await runner._poll_heartbeat_watches_once()

    manager.abandon_claim.assert_called_once_with()
    manager.confirm_claim.assert_not_called()
    assert "heartbeat delivery was not accepted" in caplog.text