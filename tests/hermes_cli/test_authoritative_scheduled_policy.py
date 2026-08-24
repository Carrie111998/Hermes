"""Required scheduled-run policy callbacks fail closed."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.conversation_loop import _restore_or_build_system_prompt
from hermes_cli import lifecycle, plugins
from hermes_cli.plugins import PluginContext, PluginManifest


def _manager(monkeypatch, callbacks=None):
    manager = plugins.PluginManager()
    manager._discovered = True
    manager._authoritative_policies["required"] = callbacks or {}
    monkeypatch.setattr(plugins, "_delivery_manager", lambda: manager)
    return manager


def _agent():
    return SimpleNamespace(
        _session_db=None, _cached_system_prompt=None,
        _build_system_prompt=lambda _message: "prompt",
        session_id="cron-session", model="test", platform="cron",
        cron_job_id="job", cron_job_name="job", cron_max_turns=12,
        runtime_policy="required",
    )


def test_authoritative_registration_is_unique_and_disposable():
    manager = plugins.PluginManager()
    context = PluginContext(PluginManifest(name="policy", source="user"), manager)
    def callback(**_kwargs):
        return {"action": "allow"}
    handle = context.register_authoritative_hook(
        "required", "on_session_start", callback)

    with pytest.raises(ValueError, match="already registered"):
        context.register_authoritative_hook(
            "required", "on_session_start", callback)
    handle.dispose()
    assert manager._authoritative_policies == {}


def test_required_admission_absent_or_raising_blocks_before_model(monkeypatch):
    monkeypatch.setattr(
        "agent.credits_tracker.seed_credits_at_session_start", lambda _agent: None,
    )
    manager = _manager(monkeypatch)
    with pytest.raises(RuntimeError, match="has no 'on_session_start'"):
        _restore_or_build_system_prompt(_agent(), None, [])

    def broken(**_kwargs):
        raise ValueError("broken admission")

    manager._authoritative_policies["required"]["on_session_start"] = broken
    with pytest.raises(ValueError, match="broken admission"):
        _restore_or_build_system_prompt(_agent(), None, [])
    assert manager._authoritative_sessions == {}


@pytest.mark.parametrize("mode", ["absent", "raising"])
def test_required_metadata_failure_prevents_mcp_rpc(monkeypatch, mode):
    import tools.mcp_tool as mcp_tool

    called = False

    class Session:
        async def call_tool(self, _name, **_kwargs):
            nonlocal called
            called = True

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def broken(**_kwargs):
        raise ValueError("broken identity")

    callbacks = {} if mode == "absent" else {"mcp_request_metadata": broken}
    manager = _manager(monkeypatch, callbacks)
    manager._authoritative_sessions["cron-session"] = "required"
    server = SimpleNamespace(session=Session(), _rpc_lock=Lock(),
                             _pending_call_context=None,
                             _mark_session_proven=lambda: None,
                             mark_tool_call=lambda: None)
    monkeypatch.setattr(mcp_tool, "_get_connected_server_for_call", lambda _name: server)
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop",
                        lambda factory, timeout: asyncio.run(factory()))

    result = mcp_tool._make_tool_handler("fleet", "claim", 5)(
        {}, session_id="cron-session", task_id="task")

    assert "Trusted MCP metadata failed" in json.loads(result)["error"]
    assert called is False


@pytest.mark.parametrize("mode", ["absent", "raising"])
def test_required_result_failure_latches_terminal_failure(monkeypatch, mode):
    import tools.mcp_tool as mcp_tool

    def broken(**_kwargs):
        raise ValueError("broken result policy")

    callbacks = {"mcp_request_metadata": lambda **_kwargs: {"meta": {}}}
    if mode == "raising":
        callbacks["mcp_tool_result"] = broken
    manager = _manager(monkeypatch, callbacks)
    manager._authoritative_sessions["cron-session"] = "required"

    class Session:
        async def call_tool(self, _name, **_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text="done")],
                                   isError=False, meta={})

    class Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    server = SimpleNamespace(session=Session(), _rpc_lock=Lock(),
                             _pending_call_context=None,
                             _mark_session_proven=lambda: None,
                             mark_tool_call=lambda: None)
    monkeypatch.setattr(mcp_tool, "_get_connected_server_for_call", lambda _name: server)
    monkeypatch.setattr(mcp_tool, "_run_on_mcp_loop",
                        lambda factory, timeout: asyncio.run(factory()))

    result = mcp_tool._make_tool_handler("fleet", "claim", 5)(
        {}, session_id="cron-session", task_id="task")

    assert "Trusted MCP result policy failed" in json.loads(result)["error"]
    assert mcp_tool.consume_mcp_runtime_stop() == {
        "reason": "policy_error", "status": "failure", "policy": "required",
    }


def test_required_finalizer_failure_has_no_receipt_and_stays_active(monkeypatch):
    def broken(**_kwargs):
        raise ValueError("broken settlement")

    manager = _manager(monkeypatch, {"on_session_finalize": broken})
    manager._authoritative_sessions["cron-session"] = "required"

    with pytest.raises(ValueError, match="broken settlement"):
        lifecycle.finalize_session(session_id="cron-session", platform="cron")
    assert manager._authoritative_sessions == {"cron-session": "required"}


def test_required_finalizer_demands_receipt(monkeypatch):
    manager = _manager(monkeypatch, {"on_session_finalize": lambda **_kwargs: None})
    manager._authoritative_sessions["cron-session"] = "required"

    with pytest.raises(RuntimeError, match="returned no receipt"):
        lifecycle.finalize_session(session_id="cron-session", platform="cron")
    assert manager._authoritative_sessions == {"cron-session": "required"}


def test_required_finalizer_cannot_be_absent(monkeypatch):
    manager = _manager(monkeypatch)
    manager._authoritative_sessions["cron-session"] = "required"

    with pytest.raises(RuntimeError, match="has no 'on_session_finalize'"):
        lifecycle.finalize_session(session_id="cron-session", platform="cron")
    assert manager._authoritative_sessions == {"cron-session": "required"}
