from __future__ import annotations

import pytest

from agent.turn_gate import (
    GateDecision,
    GateState,
    TurnGateRequest,
    acquire_outer_turn,
    build_runtime_identity,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    register_turn_gate_provider,
)
from tools.environments.local import build_subprocess_env


_ALLOWED_ENV = (
    "EXAMPLE_TURN_LEASE_ID",
    "EXAMPLE_COORDINATOR_SOCKET",
    "EXAMPLE_REQUEST_ID",
)


class _EnvironmentProvider:
    def acquire(self, request: TurnGateRequest) -> GateDecision:
        return GateDecision(
            provider_id="example-gate",
            state=GateState.OPEN,
            lease_id="example-lease",
            generation=1,
            child_environment=(
                ("EXAMPLE_TURN_LEASE_ID", "lease-from-host"),
                ("EXAMPLE_COORDINATOR_SOCKET", "/tmp/example-coordinator.sock"),
                ("EXAMPLE_REQUEST_ID", "request-123"),
            ),
        )

    def validate(self, decision: GateDecision, checkpoint: str) -> GateDecision:
        return decision

    def release(self, decision: GateDecision) -> None:
        return None


@pytest.fixture(autouse=True)
def _configured_gate():
    clear_turn_gate_registry_for_testing()
    configure_turn_gate_from_config(
        {
            "agent": {
                "turn_gate": {
                    "required_provider": "example-gate",
                    "runtime_identity": {"machine_id": "test-machine"},
                    "allowed_child_environment": list(_ALLOWED_ENV),
                }
            }
        }
    )
    register_turn_gate_provider(
        "example-gate",
        _EnvironmentProvider(),
        owner_id="example-gate",
    )
    try:
        yield
    finally:
        clear_turn_gate_registry_for_testing()


def _request() -> TurnGateRequest:
    identity = build_runtime_identity(
        surface="test",
        session_scope="session-1",
        turn_id="turn-1",
    )
    return TurnGateRequest(
        entrypoint="test",
        purpose="business",
        task_id="task-1",
        identity=identity,
    )


def test_host_environment_is_injected_only_inside_outer_turn(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TURN_LEASE_ID", "caller-controlled")
    monkeypatch.setenv("EXAMPLE_REQUEST_ID", "caller-request")

    outside = build_subprocess_env(
        base={"PATH": "/usr/bin", "EXAMPLE_TURN_LEASE_ID": "caller-controlled"},
        inherit_profile_home=False,
        scrub_secrets=False,
    )
    assert all(name not in outside for name in _ALLOWED_ENV)

    with acquire_outer_turn(_request()):
        inside = build_subprocess_env(
            base={"PATH": "/usr/bin", "EXAMPLE_TURN_LEASE_ID": "caller-controlled"},
            inherit_profile_home=False,
            scrub_secrets=False,
        )

    assert inside["EXAMPLE_TURN_LEASE_ID"] == "lease-from-host"
    assert inside["EXAMPLE_COORDINATOR_SOCKET"] == "/tmp/example-coordinator.sock"
    assert inside["EXAMPLE_REQUEST_ID"] == "request-123"


def test_host_environment_is_removed_after_outer_turn():
    with acquire_outer_turn(_request()):
        pass

    after = build_subprocess_env(
        base={"PATH": "/usr/bin", "EXAMPLE_REQUEST_ID": "spoofed"},
        inherit_profile_home=False,
        scrub_secrets=False,
    )
    assert all(name not in after for name in _ALLOWED_ENV)
