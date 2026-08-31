from __future__ import annotations

import pytest

from agent.runtime_fault_repair import retry_after_runtime_repair


def test_repair_reloads_and_retries_the_untouched_operation_once(monkeypatch):
    calls: list[object] = []

    def operation():
        calls.append("operation")
        if calls.count("operation") == 1:
            raise RuntimeError("broken contract")
        return "allowed"

    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: name == "runtime_fault_repair")
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_required_hook",
        lambda name, **kwargs: calls.append((name, kwargs)) or {"action": "RETRY", "module_prefixes": []},
    )
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda force=False: calls.append(("reload", force)))

    assert retry_after_runtime_repair(
        operation, phase="provider_request_gate", session_id="s", task_id="t", turn_id="v"
    ) == "allowed"
    assert calls.count("operation") == 2
    assert ("reload", True) in calls


def test_missing_or_declined_repair_preserves_the_original_failure(monkeypatch):
    failure = RuntimeError("do not hide me")
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: False)

    with pytest.raises(RuntimeError) as caught:
        retry_after_runtime_repair(
            lambda: (_ for _ in ()).throw(failure),
            phase="provider_request_gate", session_id="s", task_id="t", turn_id="v",
        )
    assert caught.value is failure


def test_retry_failure_is_not_repaired_twice(monkeypatch):
    repairs = 0

    def repair(name, **kwargs):
        nonlocal repairs
        repairs += 1
        return {"action": "RETRY", "module_prefixes": []}

    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_required_hook", repair)
    monkeypatch.setattr("hermes_cli.plugins.discover_plugins", lambda force=False: None)

    with pytest.raises(RuntimeError, match="still broken"):
        retry_after_runtime_repair(
            lambda: (_ for _ in ()).throw(RuntimeError("still broken")),
            phase="provider_request_gate", session_id="s", task_id="t", turn_id="v",
        )
    assert repairs == 1


def test_core_module_prefix_cannot_be_evicted(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_required_hook",
        lambda name, **kwargs: {"action": "RETRY", "module_prefixes": ["hermes_cli"]},
    )
    with pytest.raises(RuntimeError, match="broken"):
        retry_after_runtime_repair(
            lambda: (_ for _ in ()).throw(RuntimeError("broken")),
            phase="provider_request_gate", session_id="s", task_id="t", turn_id="v",
        )
