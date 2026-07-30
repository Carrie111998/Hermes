from __future__ import annotations

import json

import pytest

import agent.turn_gate as turn_gate
import hermes_cli.middleware as middleware
import model_tools


def _payload():
    return {
        "success": True,
        "name": "example-skill",
        "skill_dir": "/tmp/simulated-skill-dir",
        "content": "loaded",
    }


class _ReloadProvider:
    def acquire(self, request):
        return turn_gate.GateDecision(
            provider_id="test-gate",
            state=turn_gate.GateState.RELOAD_ONLY,
            lease_id="reload-lease",
            generation=26,
            allowed_tools=("skill_view",),
        )

    def validate(self, decision, checkpoint):
        return decision

    def release(self, decision):
        return None


def _reload_request():
    identity = turn_gate.RuntimeIdentity(
        machine_id="test-machine",
        profile="default",
        surface="conversation",
        session_instance_id="reload-session",
        gateway_instance_id="reload-gateway",
        turn_id="reload-turn",
    )
    return turn_gate.TurnGateRequest(
        entrypoint="conversation",
        purpose="reload",
        identity=identity,
    )


def _configure_reload_gate() -> None:
    turn_gate.configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": "test-gate",
                    "runtime_identity": {"machine_id": "test-machine"},
                }
            }
        }
    )


def _dispatch_skill_view(tool_call_id: str):
    return model_tools.handle_function_call(
        "skill_view",
        {"name": "example-skill"},
        task_id="task-observation",
        session_id="reload-session",
        turn_id="reload-turn",
        tool_call_id=tool_call_id,
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
    )


def test_real_registry_handler_records_successful_skill_view_observation(monkeypatch):
    observed = []
    payload = _payload()
    entry = model_tools.registry.get_entry("skill_view")
    assert entry is not None
    monkeypatch.setattr(entry, "handler", lambda args, **kwargs: json.dumps(payload))
    monkeypatch.setattr(
        turn_gate,
        "record_tool_observation",
        lambda **kwargs: observed.append(kwargs),
    )

    result = model_tools.handle_function_call(
        "skill_view",
        {"name": "example-skill"},
        task_id="task-observation",
        session_id="session-observation",
        turn_id="turn-observation",
        tool_call_id="call-observation",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
    )

    assert json.loads(result) == payload
    assert observed == [
        {
            "tool_name": "skill_view",
            "tool_args": {"name": "example-skill"},
            "tool_call_id": "call-observation",
            "result": json.dumps(payload),
        }
    ]


def test_empty_tool_call_id_poisons_reload_turn_after_real_handler(monkeypatch):
    entry = model_tools.registry.get_entry("skill_view")
    assert entry is not None
    monkeypatch.setattr(entry, "handler", lambda args, **kwargs: json.dumps(_payload()))
    turn_gate.clear_turn_gate_registry_for_testing()
    turn_gate.register_turn_gate_provider(
        "test-gate",
        _ReloadProvider(),
        owner_id="test-gate",
    )
    _configure_reload_gate()
    try:
        with turn_gate.acquire_outer_turn(_reload_request()):
            result = json.loads(_dispatch_skill_view(""))
            assert "error" in result
            with pytest.raises(turn_gate.TurnGateBlocked, match="poisoned"):
                turn_gate.enforce_tool_allowed("skill_view")
    finally:
        turn_gate.clear_turn_gate_registry_for_testing()


def test_handler_exception_poisons_reload_turn(monkeypatch):
    entry = model_tools.registry.get_entry("skill_view")
    assert entry is not None

    def fail_handler(args, **kwargs):
        raise RuntimeError("handler failed")

    monkeypatch.setattr(entry, "handler", fail_handler)
    turn_gate.clear_turn_gate_registry_for_testing()
    turn_gate.register_turn_gate_provider(
        "test-gate",
        _ReloadProvider(),
        owner_id="test-gate",
    )
    _configure_reload_gate()
    try:
        with turn_gate.acquire_outer_turn(_reload_request()):
            result = json.loads(_dispatch_skill_view("call-failed"))
            assert "error" in result
            with pytest.raises(turn_gate.TurnGateBlocked, match="poisoned"):
                turn_gate.enforce_tool_allowed("skill_view")
    finally:
        turn_gate.clear_turn_gate_registry_for_testing()


def test_execution_middleware_cannot_forge_skill_observation(monkeypatch):
    observed = []
    payload = _payload()
    monkeypatch.setattr(
        middleware,
        "run_tool_execution_middleware",
        lambda *args, **kwargs: json.dumps(payload),
    )
    monkeypatch.setattr(
        turn_gate,
        "record_tool_observation",
        lambda **kwargs: observed.append(kwargs),
    )

    result = model_tools.handle_function_call(
        "skill_view",
        {"name": "example-skill"},
        task_id="task-observation",
        session_id="session-observation",
        turn_id="turn-observation",
        tool_call_id="call-observation",
        skip_pre_tool_call_hook=True,
        skip_tool_request_middleware=True,
    )

    assert json.loads(result) == payload
    assert observed == []
