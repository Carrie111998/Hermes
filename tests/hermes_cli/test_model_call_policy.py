"""Behavioral contracts for the fail-closed model-call policy."""

import threading

import pytest

from hermes_cli.model_call_policy import (
    PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE,
    resolve_pre_model_call_policy,
)
from hermes_cli.plugins import PluginManager


def test_no_listener_allows(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)

    assert resolve_pre_model_call_policy(session_id="s1") == {
        "action": "allow",
        "message": "",
    }


def test_none_is_no_opinion(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda _name, **_payload: [None, {"action": "allow"}],
    )

    assert resolve_pre_model_call_policy(session_id="s1") == {
        "action": "allow",
        "message": "",
    }


@pytest.mark.parametrize(
    "results",
    [
        [
            {"action": "pause", "message": "wait"},
            {"action": "allow"},
            {"action": "deny", "message": "budget exhausted"},
        ],
        [
            {"action": "deny", "message": "budget exhausted"},
            {"action": "allow"},
            {"action": "pause", "message": "wait"},
        ],
    ],
)
def test_deny_wins_independent_of_registration_order(monkeypatch, results):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda _name, **_payload: results,
    )

    assert resolve_pre_model_call_policy(session_id="s1") == {
        "action": "deny",
        "message": "budget exhausted",
    }


@pytest.mark.parametrize(
    "results",
    [
        [{"action": "allow", "message": object()}],
        [{"action": "allow", "unknown": True}],
        [
            {"action": "deny", "message": "valid first"},
            {"action": "allow", "message": object()},
        ],
        [
            {"action": "pause", "message": "valid first"},
            {"action": "allow", "message": object()},
        ],
        [
            {"action": "allow", "message": object()},
            {"action": "deny", "message": "valid second"},
        ],
    ],
)
def test_every_non_none_result_is_validated_before_precedence(
    monkeypatch, results
):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda _name, **_payload: results,
    )

    assert resolve_pre_model_call_policy(session_id="s1") == {
        "action": "deny",
        "message": PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE,
    }


def test_callback_exception_and_timeout_deny(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins._resolve_hook_callback_timeout", lambda: 0.05
    )

    def boom(**_kwargs):
        raise RuntimeError("broken policy")

    manager = PluginManager()
    manager._hooks["pre_model_call_policy"] = [boom]
    monkeypatch.setattr("hermes_cli.plugins._plugin_manager", manager)

    assert resolve_pre_model_call_policy(session_id="s1") == {
        "action": "deny",
        "message": PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE,
    }

    hold = threading.Event()
    manager._hooks["pre_model_call_policy"] = [
        lambda **_kwargs: hold.wait(timeout=10.0)
    ]
    decision = resolve_pre_model_call_policy(session_id="s2")
    hold.set()

    assert decision == {
        "action": "deny",
        "message": PRE_MODEL_CALL_POLICY_FAILURE_MESSAGE,
    }
