"""Profile-specific busy-input behavior for multiplexed gateways."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.profile_routing import ProfileRoute
from gateway.run import GatewayRunner


def _runner(*, default_mode: str = "interrupt") -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._busy_input_mode = default_mode
    runner._busy_text_mode = "queue" if default_mode == "queue" else "interrupt"
    runner._busy_input_modes_by_profile = {}
    runner._busy_text_modes_by_profile = {}
    runner._profile_adapters = {}
    runner.adapters = {}
    runner._sessions = {}
    runner._running_agents = {}
    runner._adapter_for_source = lambda source: MagicMock(_pending_messages={})
    runner._draining = False
    runner._restart_requested = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda source: True
    runner._session_has_compression_in_flight = AsyncMock(return_value=False)
    return runner


def _event(*, profile: str | None) -> MessageEvent:
    return MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="dm",
            user_id="user-1",
            profile=profile,
        ),
        message_id="message-1",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secondary_mode", "handled", "expected_action"),
    [
        ("queue", False, "queue"),
        ("steer", True, "steer"),
        ("interrupt", True, "interrupt"),
    ],
)
async def test_routed_profile_busy_mode_controls_live_busy_behavior(
    monkeypatch,
    secondary_mode,
    handled,
    expected_action,
):
    """A routed target profile chooses queue/steer/interrupt independently."""
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner = _runner(default_mode="interrupt")
    runner._snapshot_profile_busy_modes(
        "research", {"display": {"busy_input_mode": secondary_mode}}
    )
    event = _event(profile="research")
    session_key = runner._session_key_for_source(event.source)
    agent = MagicMock()
    agent._active_children = []
    agent.steer.return_value = True
    runner._running_agents[session_key] = agent

    result = await runner._handle_active_session_busy_message(event, session_key)

    assert result is handled
    if expected_action == "queue":
        agent.steer.assert_not_called()
        agent.interrupt.assert_not_called()
    elif expected_action == "steer":
        agent.steer.assert_called_once_with("follow up")
        agent.interrupt.assert_not_called()
    else:
        agent.steer.assert_not_called()
        agent.interrupt.assert_called_once_with("follow up")


def test_profile_route_and_nonmultiplexed_resolution_preserve_boundaries():
    runner = _runner(default_mode="interrupt")
    runner._snapshot_profile_busy_modes(
        "research",
        {"display": {"busy_input_mode": "steer"}},
    )
    runner.config.profile_routes = [
        ProfileRoute(
            name="research-chat",
            platform="telegram",
            profile="research",
            chat_id="chat-1",
        )
    ]
    source = _event(profile=None).source

    assert runner._effective_busy_input_mode(source) == "steer"

    runner.config.multiplex_profiles = False
    source.profile = "research"
    assert runner._effective_busy_input_mode(source) == "interrupt"


@pytest.mark.parametrize("secondary_mode", [None, "not-a-mode"])
def test_missing_or_invalid_secondary_mode_falls_back_to_gateway_default(
    secondary_mode,
):
    runner = _runner(default_mode="queue")
    display = {}
    if secondary_mode is not None:
        display["busy_input_mode"] = secondary_mode
    runner._snapshot_profile_busy_modes("research", {"display": display})
    source = _event(profile="research").source

    assert runner._effective_busy_input_mode(source) == "queue"
    assert runner._effective_busy_text_mode(source) == "queue"
    assert runner._busy_input_mode == "queue"
    assert runner._busy_text_mode == "queue"


def test_legacy_busy_text_mode_is_profile_specific():
    runner = _runner(default_mode="interrupt")
    runner._snapshot_profile_busy_modes(
        "research",
        {
            "display": {
                "busy_input_mode": "interrupt",
                "busy_text_mode": "queue",
            }
        },
    )

    source = _event(profile="research").source
    assert runner._effective_busy_input_mode(source) == "interrupt"
    assert runner._effective_busy_text_mode(source) == "queue"
