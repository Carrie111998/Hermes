"""Gateway one-turn route intents are staged and consumed without runtime ownership."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from gateway.run import GatewayRunner


def _make_runner():
    """Minimal GatewayRunner with only the fields _restore_moa_one_shot reads."""
    runner = object.__new__(GatewayRunner)
    runner._session_model_overrides = {}
    runner._pending_turn_route_targets = {}
    runner._evict_cached_agent = MagicMock()
    return runner


def test_stage_moa_one_shot_queues_typed_target_without_resident_switch():
    runner = _make_runner()
    key = "agent:main:telegram:dm:typed"
    resident = {"provider": "openrouter", "model": "gpt-4"}
    runner._session_model_overrides[key] = resident.copy()
    event = SimpleNamespace(text="/moa compare these")

    runner._stage_moa_turn_route_target(
        event,
        key,
        preset="deep",
        payload="compare these",
    )

    assert event.text == "compare these"
    assert runner._pending_turn_route_targets[key] == {
        "kind": "moa",
        "preset": "deep",
    }
    assert runner._session_model_overrides[key] == resident
    assert not hasattr(event, "_moa_disable_after_turn")
    runner._evict_cached_agent.assert_not_called()


def test_gateway_one_turn_moa_uses_only_moa_intent_flag():
    runner = _make_runner()
    key = "agent:main:telegram:dm:moa-flags"
    runner._pending_turn_route_targets[key] = {"kind": "moa", "preset": "deep"}
    agent = SimpleNamespace(
        session_id="session-moa",
        provider="kimi-coding",
        model="k3",
    )

    request = runner._take_gateway_turn_routing_request(
        session_key=key,
        agent=agent,
        user_text="compare",
        api_user_message="compare",
        persist_user_message="compare",
    )

    assert request.explicit_turn_override is False
    assert request.explicit_moa_override is True
    assert request.explicit_target == {"kind": "moa", "preset": "deep"}
    assert key not in runner._pending_turn_route_targets


def test_take_gateway_turn_routing_request_consumes_typed_intent_once():
    runner = _make_runner()
    key = "agent:main:discord:dm:typed"
    runner._pending_turn_route_targets[key] = {
        "kind": "model",
        "provider": "openrouter",
        "model": "target/model",
    }
    runner._session_model_overrides[key] = {
        "provider": "openrouter",
        "model": "resident/model",
    }
    agent = SimpleNamespace(
        session_id="session-1",
        provider="openrouter",
        model="resident/model",
    )

    request = runner._take_gateway_turn_routing_request(
        session_key=key,
        agent=agent,
        user_text="route this",
        api_user_message="[API-only context]\nroute this",
        persist_user_message="route this",
    )

    assert key not in runner._pending_turn_route_targets
    assert request.surface == "gateway"
    assert request.explicit_turn_override is True
    assert request.explicit_moa_override is False
    assert request.explicit_target == {
        "kind": "model",
        "provider": "openrouter",
        "model": "target/model",
    }
    assert request.session_pinned is True
    assert request.prepare_user_message is not None
    prepared = request.prepare_user_message("ignored")
    assert prepared.user_message == "[API-only context]\nroute this"
    assert prepared.persist_user_message == "route this"

    second = runner._take_gateway_turn_routing_request(
        session_key=key,
        agent=agent,
        user_text="next",
        api_user_message="next",
        persist_user_message="next",
    )
    assert second.explicit_target is None
    assert second.explicit_turn_override is False


def test_conversation_reset_discards_unconsumed_typed_intent():
    runner = _make_runner()
    key = "agent:main:telegram:dm:reset"
    other = "agent:main:telegram:dm:other"
    runner._pending_turn_route_targets[key] = {
        "kind": "moa",
        "preset": "deep",
    }
    runner._pending_turn_route_targets[other] = {
        "kind": "model",
        "provider": "openrouter",
        "model": "keep/model",
    }
    runner._clear_conversation_scope(key, reason="test_reset")

    assert key not in runner._pending_turn_route_targets
    assert runner._pending_turn_route_targets[other]["model"] == "keep/model"
    runner._evict_cached_agent.assert_not_called()
