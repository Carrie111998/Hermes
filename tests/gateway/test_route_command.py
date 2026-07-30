from types import SimpleNamespace

import pytest
import yaml

from agent.turn_routing_runtime import TurnRoutingSessionState
from gateway.run import GatewayRunner


class _Event:
    source = SimpleNamespace(platform="test", chat_id="chat", user_id="user")

    def __init__(self, argument):
        self._argument = argument

    def get_command_args(self):
        return self._argument


@pytest.mark.asyncio
async def test_gateway_route_reset_is_conversation_scoped(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    state_a = TurnRoutingSessionState(
        affinity_route="deep",
        affinity_remaining=2,
        fail_off=True,
    )
    state_b = TurnRoutingSessionState(
        affinity_route="fast",
        affinity_remaining=1,
    )
    runner._session_state(
        "session-a"
    ).conversation.turn_routing_session_state = state_a
    runner._session_state(
        "session-b"
    ).conversation.turn_routing_session_state = state_b
    runner._session_key_for_source = lambda _source: "session-a"
    monkeypatch.setattr(
        "gateway.slash_commands.load_turn_routing_config",
        lambda: {"mode": "observe", "budget": {}},
        raising=False,
    )

    output = await runner._handle_route_command(_Event("reset"))

    assert output == "Session routing state reset; route mode remains observe"
    assert state_a.affinity_route is None
    assert state_a.fail_off is False
    assert state_b.affinity_route == "fast"


@pytest.mark.asyncio
async def test_gateway_route_mode_writes_only_profile_routing_config(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"routing": {"mode": "off"}, "model": {"default": "k3"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: "session-a"

    output = await runner._handle_route_command(_Event("observe"))

    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert output == "Route mode set to observe"
    assert written["routing"]["mode"] == "observe"
    assert written["model"]["default"] == "k3"
    state = runner._session_state(
        "session-a"
    ).conversation.turn_routing_session_state
    assert state.latest_payload is None