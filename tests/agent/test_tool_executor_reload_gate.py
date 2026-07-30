from __future__ import annotations

import json

import pytest

import agent.turn_gate as turn_gate
import model_tools
from agent.tool_executor import _host_gate_block_message
from agent.turn_gate import (
    GateDecision,
    GateState,
    RuntimeIdentity,
    TurnGateRequest,
    acquire_outer_turn,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    register_turn_gate_provider as _register_turn_gate_provider,
)


def _configure_gate(provider_id: str) -> None:
    configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": provider_id,
                    "runtime_identity": {"machine_id": "test-machine"},
                }
            }
        }
    )


def register_turn_gate_provider(provider_id: str, provider) -> None:
    _register_turn_gate_provider(
        provider_id,
        provider,
        owner_id=provider_id,
    )


class ReloadOnlyProvider:
    def acquire(self, request):
        return GateDecision(
            provider_id="test-gate",
            state=GateState.RELOAD_ONLY,
            lease_id="lease-reload",
            generation=26,
            allowed_tools=("skill_view",),
        )

    def validate(self, decision, checkpoint):
        return decision

    def release(self, decision):
        return None


@pytest.fixture(autouse=True)
def gate():
    clear_turn_gate_registry_for_testing()
    register_turn_gate_provider("test-gate", ReloadOnlyProvider())
    _configure_gate("test-gate")
    yield
    clear_turn_gate_registry_for_testing()


def reload_request() -> TurnGateRequest:
    identity = RuntimeIdentity(
        machine_id="test-machine",
        profile="default",
        surface="gateway",
        session_instance_id="test-session",
        gateway_instance_id="test-gateway",
        turn_id="test-turn",
    )
    return TurnGateRequest(
        entrypoint="gateway",
        purpose="reload",
        identity=identity,
    )


def test_tool_executor_host_gate_allows_only_exact_reload_tool():
    with acquire_outer_turn(reload_request()):
        assert _host_gate_block_message("skill_view") is None
        blocked = _host_gate_block_message("terminal")
    assert "RELOAD_ONLY" in blocked
    assert "generation=26" in blocked
    assert "terminal" in blocked


def test_registry_dispatch_is_blocked_before_argument_coercion(monkeypatch):
    def forbidden_coercion(*args, **kwargs):
        raise AssertionError("host gate must run before argument coercion or dispatch")

    monkeypatch.setattr(model_tools, "coerce_tool_args", forbidden_coercion)
    with acquire_outer_turn(reload_request()):
        result = model_tools.handle_function_call(
            "terminal",
            {"command": "must-not-run"},
            task_id="task-1",
        )
    payload = json.loads(result)
    assert payload["error_type"] == "turn_gate_block"
    assert "RELOAD_ONLY" in payload["error"]


def test_direct_tool_dispatch_without_required_outer_lease_fails_closed(monkeypatch):
    def forbidden_coercion(*args, **kwargs):
        raise AssertionError("dispatch must not begin without an outer lease")

    monkeypatch.setattr(model_tools, "coerce_tool_args", forbidden_coercion)
    result = model_tools.handle_function_call(
        "terminal",
        {"command": "must-not-run"},
        task_id="task-direct",
    )
    payload = json.loads(result)
    assert payload["error_type"] == "turn_gate_block"
    assert "outer-turn lease" in payload["error"]


def test_direct_registry_dispatch_fails_closed_when_gate_check_raises(monkeypatch):
    entry = model_tools.registry.get_entry("skill_view")
    assert entry is not None
    executed = []
    monkeypatch.setattr(
        entry,
        "handler",
        lambda args, **kwargs: executed.append(True) or json.dumps({"executed": True}),
    )
    monkeypatch.setattr(
        turn_gate,
        "tool_block_message",
        lambda name: (_ for _ in ()).throw(RuntimeError("gate unavailable")),
    )

    result = model_tools.registry.dispatch(
        "skill_view",
        {"name": "must-not-run"},
        task_id="task-direct-registry",
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert executed == []
    assert payload["error_type"] == "turn_gate_block"
    assert "failed closed" in payload["error"]
