import pytest
from unittest.mock import MagicMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class _FakeSessionEntry:
    session_id = "sid-gateway-goal-config"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-config"


@pytest.mark.asyncio
async def test_gateway_goal_uses_goals_max_turns_from_full_config(tmp_path, monkeypatch):
    """Gateway /goal should honor top-level goals.max_turns from config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    runner._schedule_adapter_semantic_base_refresh = MagicMock()

    event = MessageEvent(
        text="/goal ship the benchmark",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set (7-turn budget): ship the benchmark" in response
        state = goals.GoalManager("sid-gateway-goal-config").state
        assert state is not None
        assert state.max_turns == 7
        runner._schedule_adapter_semantic_base_refresh.assert_called_once_with(
            event.source, "sid-gateway-goal-config"
        )
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "initial_status", "expected_status"),
    [
        ("pause", "active", "paused"),
        ("resume", "paused", "active"),
        ("clear", "active", "cleared"),
        ("stop", "active", "cleared"),
        ("done", "active", "cleared"),
    ],
)
async def test_goal_status_changes_refresh_semantic_base_after_persistence(
    tmp_path, monkeypatch, command, initial_status, expected_status
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    manager = goals.GoalManager("sid-gateway-goal-config")
    manager.set("ship the benchmark")
    if initial_status == "paused":
        manager.pause()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    observed_statuses = []
    runner._schedule_adapter_semantic_base_refresh = MagicMock(
        side_effect=lambda _source, sid: observed_statuses.append(
            goals.GoalManager(sid).state.status
        )
    )
    event = MessageEvent(
        text=f"/goal {command}",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
    )

    try:
        await GatewayRunner._handle_goal_command(runner, event)
        assert observed_statuses == [expected_status]
        runner._schedule_adapter_semantic_base_refresh.assert_called_once_with(
            event.source, "sid-gateway-goal-config"
        )
    finally:
        goals._DB_CACHE.clear()
