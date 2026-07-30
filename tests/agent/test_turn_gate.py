from __future__ import annotations

from typing import Any, Iterator

import asyncio
from dataclasses import replace

import pytest

from agent.turn_gate import (
    GateDecision,
    GateState,
    RuntimeIdentity,
    TurnGateBlocked,
    TurnGateRequest,
    acquire_outer_turn,
    build_runtime_identity,
    clear_turn_gate_registry_for_testing,
    configure_turn_gate_from_config,
    create_detached_task,
    current_turn_gate_decision,
    enforce_output_allowed,
    enforce_tool_allowed,
    inject_turn_gate_child_environment,
    register_turn_gate_provider,
)


class FakeProvider:
    def __init__(self, decision: GateDecision) -> None:
        self.decision = decision
        self.acquire_calls = 0
        self.validate_calls: list[str] = []
        self.release_calls = 0

    def acquire(self, request: TurnGateRequest) -> GateDecision:
        self.acquire_calls += 1
        return self.decision

    def validate(self, decision: GateDecision, checkpoint: str) -> GateDecision:
        self.validate_calls.append(checkpoint)
        return self.decision

    def release(self, decision: GateDecision) -> None:
        self.release_calls += 1


@pytest.mark.parametrize("method_name", ["acquire", "validate", "release"])
def test_async_provider_methods_are_rejected_at_registration(method_name: str) -> None:
    class AsyncProvider(FakeProvider):
        pass

    async def async_method(*args, **kwargs):
        return None

    setattr(AsyncProvider, method_name, async_method)
    provider = AsyncProvider(_decision())

    with pytest.raises(ValueError, match="synchronous"):
        register_turn_gate_provider(
            "example-gate",
            provider,
            owner_id="example-gate",
        )


def _coroutine_returning(value: object) -> Any:
    async def work():
        return value

    return work()


def test_sync_wrapper_returning_async_acquire_fails_closed() -> None:
    class Provider(FakeProvider):
        def acquire(self, request: TurnGateRequest) -> Any:
            return _coroutine_returning(self.decision)

    _configure()
    _register(Provider(_decision()))
    with pytest.raises(TurnGateBlocked, match="async work during acquire"):
        with acquire_outer_turn(_request()):
            pass


def test_sync_wrapper_returning_async_validate_fails_closed() -> None:
    class Provider(FakeProvider):
        def validate(self, decision: GateDecision, checkpoint: str) -> Any:
            return _coroutine_returning(self.decision)

    _configure()
    _register(Provider(_decision()))
    with acquire_outer_turn(_request()):
        with pytest.raises(TurnGateBlocked, match="async work during validate"):
            enforce_tool_allowed("terminal")


def test_sync_wrapper_returning_async_release_fails_closed() -> None:
    class Provider(FakeProvider):
        def release(self, decision: GateDecision) -> Any:
            return _coroutine_returning(None)

    _configure()
    _register(Provider(_decision()))
    with pytest.raises(TurnGateBlocked, match="failed during release"):
        with acquire_outer_turn(_request()):
            pass


@pytest.fixture(autouse=True)
def _clear_gate() -> Iterator[None]:
    clear_turn_gate_registry_for_testing()
    yield
    clear_turn_gate_registry_for_testing()


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        machine_id="machine-1",
        profile="default",
        surface="test",
        session_instance_id="session-instance-1",
        gateway_instance_id="gateway-instance-1",
        turn_id="turn-1",
    )


def _request(*, purpose: str = "business") -> TurnGateRequest:
    return TurnGateRequest(
        entrypoint="test",
        purpose=purpose,
        task_id="task-1",
        identity=_identity(),
    )


def _decision(
    *,
    state: GateState = GateState.OPEN,
    generation: int = 7,
    allowed_tools: tuple[str, ...] = (),
    child_environment: tuple[tuple[str, str], ...] = (),
) -> GateDecision:
    return GateDecision(
        provider_id="example-gate",
        state=state,
        lease_id="lease-1",
        generation=generation,
        allowed_tools=allowed_tools,
        child_environment=child_environment,
    )


def _configure(*, allowed_child_environment: list[str] | None = None) -> None:
    gate: dict[str, object] = {
        "required_provider": "example-gate",
        "runtime_identity": {"machine_id": "machine-1"},
    }
    if allowed_child_environment is not None:
        gate["allowed_child_environment"] = allowed_child_environment
    configure_turn_gate_from_config({"agent": {"turn_gate": gate}})


def _register(provider: FakeProvider) -> None:
    register_turn_gate_provider(
        "example-gate",
        provider,
        owner_id="example-gate",
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_id", 1),
        ("lease_id", object()),
        ("state", "OPEN"),
        ("generation", True),
        ("generation", 1.5),
        ("allowed_tools", ["skill_view"]),
        ("allowed_tools", ("skill_view", "skill_view")),
        ("child_environment", [("LEASE_ID", "lease-1")]),
        ("child_environment", (("BAD-NAME", "value"),)),
        ("child_environment", (("LEASE_ID", "value\x00tail"),)),
    ],
)
def test_gate_decision_rejects_truthy_wrong_types(field: str, value: object) -> None:
    kwargs: dict[str, Any] = {
        "provider_id": "example-gate",
        "state": GateState.OPEN,
        "lease_id": "lease-1",
        "generation": 1,
        "allowed_tools": (),
        "child_environment": (),
    }
    kwargs[field] = value
    with pytest.raises(ValueError):
        GateDecision(**kwargs)


def test_required_provider_missing_fails_closed_before_body() -> None:
    _configure()
    body_ran = False

    with pytest.raises(TurnGateBlocked, match="not registered"):
        with acquire_outer_turn(_request()):
            body_ran = True

    assert body_ran is False


def test_outer_turn_reuses_one_lease_and_releases_once() -> None:
    _configure()
    provider = FakeProvider(_decision())
    _register(provider)

    with acquire_outer_turn(_request()) as outer:
        with acquire_outer_turn(_request()) as nested:
            assert nested is outer
            enforce_tool_allowed("terminal")
            enforce_output_allowed()

    assert provider.acquire_calls == 1
    assert provider.release_calls == 1
    assert provider.validate_calls == ["tool:terminal", "output"]


def test_reload_only_uses_exact_tool_allowlist_and_blocks_output() -> None:
    _configure()
    provider = FakeProvider(
        _decision(state=GateState.RELOAD_ONLY, allowed_tools=("skill_view",))
    )
    _register(provider)

    with acquire_outer_turn(_request(purpose="reload")):
        enforce_tool_allowed("skill_view")
        with pytest.raises(TurnGateBlocked, match="RELOAD_ONLY"):
            enforce_tool_allowed("read_file")
        with pytest.raises(TurnGateBlocked, match="poisoned"):
            enforce_output_allowed()


def test_checkpoint_drift_poison_survives_provider_recovery() -> None:
    _configure()
    provider = FakeProvider(_decision())
    _register(provider)

    with acquire_outer_turn(_request()):
        provider.decision = replace(provider.decision, generation=8)
        with pytest.raises(TurnGateBlocked, match="generation changed"):
            enforce_tool_allowed("terminal")
        provider.decision = replace(provider.decision, generation=7)
        with pytest.raises(TurnGateBlocked, match="poisoned"):
            enforce_output_allowed()


def test_direct_tool_and_output_without_required_outer_lease_fail_closed() -> None:
    _configure()
    _register(FakeProvider(_decision()))

    with pytest.raises(TurnGateBlocked, match="active outer-turn lease"):
        enforce_tool_allowed("terminal")
    with pytest.raises(TurnGateBlocked, match="active outer-turn lease"):
        enforce_output_allowed()


def test_child_environment_is_provider_owned_allowlisted_and_spoof_resistant() -> None:
    _configure(allowed_child_environment=["LEASE_ID", "EIP_ID"])
    provider = FakeProvider(
        _decision(child_environment=(("LEASE_ID", "lease-value"),))
    )
    _register(provider)
    base = {"LEASE_ID": "spoofed", "EIP_ID": "spoofed", "PATH": "/usr/bin"}

    with acquire_outer_turn(_request()):
        child = inject_turn_gate_child_environment(base)

    assert child == {"LEASE_ID": "lease-value", "PATH": "/usr/bin"}
    assert base["LEASE_ID"] == "spoofed"
    assert provider.validate_calls == ["child-environment"]


def test_provider_environment_outside_host_allowlist_fails_closed() -> None:
    _configure(allowed_child_environment=["LEASE_ID"])
    provider = FakeProvider(_decision(child_environment=(("EIP_ID", "eip-1"),)))
    _register(provider)

    with acquire_outer_turn(_request()):
        with pytest.raises(TurnGateBlocked, match="allowlist"):
            inject_turn_gate_child_environment({})


def test_child_environment_preserves_legacy_values_but_rejects_non_text_keys() -> None:
    _configure(allowed_child_environment=["LEASE_ID"])
    sentinel = object()

    legacy_base: Any = {"LEASE_ID": "spoofed", "LEGACY_VALUE": sentinel}
    child = inject_turn_gate_child_environment(legacy_base)

    assert "LEASE_ID" not in child
    assert child["LEGACY_VALUE"] is sentinel
    with pytest.raises(ValueError, match="text names"):
        inject_turn_gate_child_environment({1: "invalid"})  # type: ignore[dict-item]


@pytest.mark.asyncio
async def test_detached_task_does_not_inherit_outer_turn_context() -> None:
    _configure()
    provider = FakeProvider(_decision())
    _register(provider)

    async def probe() -> GateDecision | None:
        await asyncio.sleep(0)
        return current_turn_gate_decision()

    with acquire_outer_turn(_request()):
        result = await create_detached_task(probe())

    assert result is None


def test_malformed_config_latches_fail_closed_until_valid_reload() -> None:
    with pytest.raises(TurnGateBlocked, match="required_provider"):
        configure_turn_gate_from_config(
            {"agent": {"turn_gate": {"required_provider": True}}}
        )

    with pytest.raises(TurnGateBlocked, match="configuration"):
        enforce_output_allowed()

    _configure()
    provider = FakeProvider(_decision())
    _register(provider)
    with acquire_outer_turn(_request()):
        enforce_output_allowed()


def test_provider_identity_must_match_plugin_manifest_owner() -> None:
    with pytest.raises(ValueError, match="manifest owner"):
        register_turn_gate_provider(
            "example-gate",
            FakeProvider(_decision()),
            owner_id="different-plugin",
        )


def test_host_identity_builder_uses_configured_machine_and_opaque_session() -> None:
    _configure()

    first = build_runtime_identity(
        surface="gateway",
        session_scope="raw-chat:123",
        turn_id="turn-a",
    )
    second = build_runtime_identity(
        surface="gateway",
        session_scope="raw-chat:123",
        turn_id="turn-b",
    )

    assert first is not None and second is not None
    assert first.machine_id == "machine-1"
    assert first.profile == "default"
    assert first.session_instance_id == second.session_instance_id
    assert first.session_instance_id != "raw-chat:123"
    assert first.gateway_instance_id == second.gateway_instance_id
    assert first.turn_id == "turn-a"
    assert second.turn_id == "turn-b"
