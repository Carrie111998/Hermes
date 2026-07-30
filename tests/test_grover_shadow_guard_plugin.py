"""The shadow guard blocks agent/tool side effects without changing prod."""

from __future__ import annotations

from unittest.mock import Mock

import pytest


def test_shadow_profile_blocks_every_tool_call(monkeypatch):
    from plugins.grover_shadow_guard import _on_pre_tool_call

    monkeypatch.setenv("HERMES_PROFILE", "grover-shadow")
    directive = _on_pre_tool_call(tool_name="terminal", args={"command": "anything"})

    assert directive == {
        "action": "block",
        "message": "grover-shadow is mechanically external-effect-free",
    }


def test_shadow_role_blocks_llm_and_tool_execution_without_calling_downstream(
    monkeypatch,
):
    from plugins.grover_shadow_guard import (
        _on_llm_execution,
        _on_tool_execution,
    )

    monkeypatch.setenv("GROVER_RUNTIME_ROLE", "shadow")
    downstream = Mock(side_effect=AssertionError("downstream external effect ran"))

    assert _on_llm_execution(request={}, next_call=downstream) == {
        "error": "grover-shadow external effects disabled"
    }
    assert _on_tool_execution(
        tool_name="send_message", args={}, next_call=downstream
    ) == {"error": "grover-shadow external effects disabled"}
    downstream.assert_not_called()


def test_guard_does_not_weaken_production(monkeypatch):
    from plugins.grover_shadow_guard import (
        _on_llm_execution,
        _on_pre_tool_call,
        _on_tool_execution,
    )

    monkeypatch.setenv("HERMES_PROFILE", "grover-prod")
    monkeypatch.setenv("GROVER_RUNTIME_ROLE", "prod")
    downstream = Mock(return_value={"ok": True})

    assert _on_pre_tool_call(tool_name="terminal", args={}) is None
    assert _on_llm_execution(request={"x": 1}, next_call=downstream) == {"ok": True}
    assert _on_tool_execution(
        tool_name="terminal", args={"x": 1}, next_call=downstream
    ) == {"ok": True}
    assert downstream.call_count == 2


def test_guard_registers_profile_bound_policy_and_execution_boundaries(monkeypatch):
    from plugins.grover_shadow_guard import register

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("GROVER_RUNTIME_ROLE", raising=False)
    context = Mock(profile_name="grover-shadow")
    register(context)

    hook_names = [call.args[0] for call in context.register_hook.call_args_list]
    middleware_names = [
        call.args[0] for call in context.register_middleware.call_args_list
    ]
    assert hook_names == ["pre_tool_call"]
    assert middleware_names == ["llm_execution", "tool_execution"]

    downstream = Mock(side_effect=AssertionError("profile-bound guard was bypassed"))
    llm_guard = context.register_middleware.call_args_list[0].args[1]
    tool_guard = context.register_middleware.call_args_list[1].args[1]
    assert llm_guard(request={}, next_call=downstream)["error"]
    assert tool_guard(tool_name="terminal", args={}, next_call=downstream)["error"]
    downstream.assert_not_called()


def test_guard_refuses_registration_outside_shadow_profile():
    from plugins.grover_shadow_guard import register

    with pytest.raises(RuntimeError, match="only be enabled for grover-shadow"):
        register(Mock(profile_name="grover-prod"))
