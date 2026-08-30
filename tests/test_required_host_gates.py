"""Fail-closed lifecycle seams used by an external Agent Harness."""

import pytest

from hermes_cli.plugins import PluginManager, VALID_HOOKS


def _manager(callbacks):
    manager = object.__new__(PluginManager)
    manager._hooks = callbacks
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
