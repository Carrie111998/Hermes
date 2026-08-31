"""Fail-closed lifecycle seams used by an external Agent Harness."""

import pytest

from hermes_cli.plugins import PluginManager, VALID_HOOKS


def _manager(callbacks):
    manager = object.__new__(PluginManager)
    manager._hooks = callbacks
    manager._hook_applies = {}
    return manager


def test_required_gate_names_are_registered() -> None:
    assert {
        "provider_request_gate",
        "assistant_final_candidate_gate",
        "assistant_persist_gate",
        "assistant_persist_receipt",
    } <= VALID_HOOKS


def test_required_gate_has_one_owner_and_propagates_failure() -> None:
    def broken(**kwargs):
        raise ValueError("stop")

    manager = _manager({"provider_request_gate": [broken]})
    with pytest.raises(ValueError, match="stop"):
        manager.invoke_required_hook("provider_request_gate", session_id="s")

    manager = _manager(
        {"provider_request_gate": [lambda **_: None, lambda **_: None]}
    )
    with pytest.raises(RuntimeError, match="exactly one owner"):
        manager.invoke_required_hook("provider_request_gate")


def test_absent_required_gate_preserves_generic_hermes() -> None:
    manager = _manager({})
    assert manager.invoke_required_hook("provider_request_gate") is None


def test_required_gate_applies_false_is_a_complete_turn_bypass() -> None:
    called = []

    def gate(**_kwargs):
        called.append(True)
        return {"action": "ALLOW"}

    manager = _manager({"assistant_persist_gate": [gate]})
    manager._hook_applies[("assistant_persist_gate", id(gate))] = (
        lambda turn_id="", **_: turn_id == "formal"
    )

    assert manager.has_applicable_required_hook(
        "assistant_persist_gate", turn_id="ordinary"
    ) is False
    assert manager.invoke_required_hook(
        "assistant_persist_gate", turn_id="ordinary"
    ) is None
    assert called == []
    assert manager.invoke_required_hook(
        "assistant_persist_gate", turn_id="formal"
    ) == {"action": "ALLOW"}


def test_applies_failure_arms_required_gate_fail_closed() -> None:
    def gate(**_kwargs):
        return {"action": "ALLOW"}

    def broken(**_kwargs):
        raise ValueError("predicate failed")

    manager = _manager({"assistant_persist_gate": [gate]})
    manager._hook_applies[("assistant_persist_gate", id(gate))] = broken

    assert manager.has_applicable_required_hook(
        "assistant_persist_gate", turn_id="formal"
    ) is True
    with pytest.raises(ValueError, match="predicate failed"):
        manager.invoke_required_hook(
            "assistant_persist_gate", turn_id="formal"
        )
