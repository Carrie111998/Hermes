"""Lifecycle-scoped gateway delivery regressions for terminal completions.

The gateway contract here is deliberately narrower than exactly-once: one live
GatewayRunner suppresses concurrent/replayed copies after successful adapter
injection, failed injection remains retryable, and durable async-delegation
state (when available) is acknowledged through its authoritative SQLite API.
"""

import asyncio
import json
import queue
import threading
import time
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner, _drain_gateway_watch_events
from gateway.session import SessionSource
from tools.process_registry import ProcessRegistry, ProcessSession


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path, monkeypatch):
    """Any current/future durable compatibility path must stay in tmp state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import tools.process_registry as pr_module

    monkeypatch.setattr(pr_module, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr_module.ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    return registry


def _runner(adapter, *, origins=None):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.session_store = SimpleNamespace(
        _ensure_loaded=lambda: None,
        _entries=origins or {},
    )
    runner._session_source_cache = {}
    runner._completion_delivery_lock = __import__("threading").Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = OrderedDict()
    runner._completion_delivery_retention = 2048
    return runner


def _async_event(delegation_id="deleg_duplicate"):
    return {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": "agent:main:telegram:dm:12345:678",
        "goal": "Investigate flaky test",
        "status": "completed",
        "summary": "Found it",
        "api_calls": 1,
        "duration_seconds": 12.0,
        "dispatched_at": 1000.0,
        "completed_at": 1012.0,
        # PR #62479 stamps these on gateway-owned events. They must not
        # change the producer identity used for queue replay.
        "origin_profile": "default",
        "origin_hermes_home": "/tmp/hermes-default",
    }


def _delegated_approval_event(approval_id="approval-dynamic"):
    return {
        "type": "delegated_approval_request",
        "approval_id": approval_id,
        "delegation_id": "deleg-dynamic",
        "session_key": "agent:main:telegram:dm:12345:678",
        "subagent_id": "subagent-dynamic",
        "child_session_id": "child-dynamic",
        "command": "python -c 'print(1)'",
        "command_digest": "d" * 64,
        "tool_call_id": "call-dynamic",
        "pattern_keys": ["script execution via -e/-c flag"],
        "expires_in_seconds": 90,
        "untrusted_data": True,
        "system_authored": True,
    }


def _completion_event(*, started_at, session_id="proc_reused"):
    return {
        "type": "completion",
        "session_id": session_id,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "started_at": started_at,
        "command": "echo done",
        "exit_code": 0,
        "completion_reason": "exited",
        "output": "done\n",
    }


def _stop_after_sleeps(monkeypatch, runner, count):
    sleep_calls = 0

    async def _bounded_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= count:
            runner._running = False

    monkeypatch.setattr(asyncio, "sleep", _bounded_sleep)


def test_duplicate_async_queue_replay_injects_once(monkeypatch, isolated_registry):
    """Byte-identical queue replays produce one turn in one gateway lifecycle."""
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(dict(_async_event()))
    isolated.put(dict(_async_event()))

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()


def test_delegated_approval_event_uses_gateway_fresh_turn_delivery(
    monkeypatch, isolated_registry
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_delegated_approval_event())

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_awaited_once()
    incoming = adapter.handle_message.await_args.args[0]
    assert incoming.text.startswith("[SYSTEM EVENT: delegated_approval_request]")
    assert "UNTRUSTED DATA" in incoming.text


def test_post_turn_drain_preserves_delegated_request_for_single_gateway_owner():
    isolated = queue.Queue()
    completion = _completion_event(started_at=1.0)
    approval = _delegated_approval_event()
    watch = {"type": "watch_match", "session_id": "p", "pattern": "ready"}
    for event in (completion, approval, watch):
        isolated.put(event)

    assert _drain_gateway_watch_events(isolated) == [watch]
    remaining = [isolated.get_nowait()]
    assert remaining == [approval]
    assert isolated.empty()


def test_post_turn_drain_does_not_publish_duplicate_delegated_request():
    isolated = queue.Queue()
    approval = _delegated_approval_event()
    isolated.put(approval)
    assert _drain_gateway_watch_events(isolated) == []
    assert isolated.get_nowait() is approval
    assert isolated.empty()


def test_commissioning_turn_request_survives_drain_and_resumes_one_completion(
    monkeypatch, isolated_registry
):
    """Full deterministic queue path: pause, fresh turn, resume, completion."""
    from agent.delegation_context import delegated_approval_context
    from tools import approval
    from tools import delegated_approval as da
    from tools.delegate_tool import _active_subagents, _active_subagents_lock

    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )

    parent = object()
    child = object()
    authority = da.DelegatedApprovalAuthority(
        owner_agent=parent,
        child_agent=child,
        subagent_id="subagent-commissioned",
        child_session_id="child-commissioned",
        parent_session_id="parent-commissioned",
        owner_approval_session_key="agent:main:telegram:dm:12345:678",
        owner_session_id=None,
        owner_transport=None,
        owner_session_record=None,
        delegation_id="deleg-commissioned",
        parent_lane_enabled=True,
        parent_task_id="task-commissioned",
        delegated_goal="commission deterministic child",
    )
    with _active_subagents_lock:
        _active_subagents[authority.subagent_id] = {
            "subagent_id": authority.subagent_id,
            "agent": child,
            "owner_agent": parent,
            "approval_authority": authority,
            "accepting_steer": True,
        }

    child_result = {}
    command = "python -c 'print(1 + 2)'"

    def _child_run():
        token = approval._approval_tool_call_id.set("call-commissioned")
        try:
            with delegated_approval_context(authority):
                child_result.update(approval.check_all_command_guards(command, "local"))
            if child_result.get("approved"):
                isolated.put({
                    **_async_event("deleg-final-completion"),
                    "status": "completed",
                    "summary": "one final child completion",
                })
        finally:
            approval._approval_tool_call_id.reset(token)

    child_thread = threading.Thread(target=_child_run, daemon=True)
    child_thread.start()
    deadline = time.monotonic() + 1
    while isolated.empty() and time.monotonic() < deadline:
        time.sleep(0.002)
    assert not isolated.empty() and child_thread.is_alive()

    assert _drain_gateway_watch_events(isolated) == []
    assert isolated.qsize() == 1

    delivered_texts = []

    async def _handle_message(event):
        delivered_texts.append(event.text)
        if event.text.startswith("[SYSTEM EVENT: delegated_approval_request]"):
            payload = json.loads(event.text.splitlines()[2])
            assert da.resolve_parent_decision(
                parent, payload["approval_id"], "once"
            )["resolved"]

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_handle_message))
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)
    asyncio.run(runner._async_delegation_watcher(interval=0))
    child_thread.join(1)

    assert child_result.get("parent_agent_approved") is True
    assert sum(text.startswith("[SYSTEM EVENT: delegated_approval_request]") for text in delivered_texts) == 1
    assert sum("one final child completion" in text for text in delivered_texts) == 1
    assert len(delivered_texts) == 2
    assert isolated.empty()
    with _active_subagents_lock:
        _active_subagents.pop(authority.subagent_id, None)


def test_unroutable_async_event_is_not_requeued_forever(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    event = _async_event("deleg_desktop_or_cli")
    event["session_key"] = "20260711_unparseable_ui_session"
    isolated.put(event)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=2)

    asyncio.run(runner._async_delegation_watcher(interval=0))

    adapter.handle_message.assert_not_awaited()
    assert isolated.empty()


def test_concurrent_claims_share_the_same_narrow_delivery_seam():
    """Concurrent consumers in one runner cannot both enter the adapter."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_injection(_event):
        entered.set()
        await release.wait()

    adapter = SimpleNamespace(handle_message=AsyncMock(side_effect=_blocked_injection))
    runner = _runner(adapter)
    event = _async_event()
    text = "completion"

    async def _exercise():
        first = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await entered.wait()
        second = asyncio.create_task(runner._deliver_completion_notification(text, dict(event)))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    assert sorted(asyncio.run(_exercise()), key=str) == [None, True]
    adapter.handle_message.assert_awaited_once()


def test_failed_async_injection_is_retried_and_only_success_is_acked(
    monkeypatch, isolated_registry,
):
    isolated = queue.Queue()
    monkeypatch.setattr(isolated_registry, "completion_queue", isolated)
    isolated.put(_async_event())

    adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=[RuntimeError("temporary"), None])
    )
    runner = _runner(adapter)
    _stop_after_sleeps(monkeypatch, runner, count=3)

    from tools import async_delegation

    acknowledgements = []
    monkeypatch.setattr(
        async_delegation,
        "complete_completion_delivery",
        lambda delegation_id, _claim_id: acknowledgements.append(delegation_id) or True,
        raising=False,
    )

    asyncio.run(runner._async_delegation_watcher(interval=0))

    assert adapter.handle_message.await_count == 2
    assert acknowledgements == ["deleg_duplicate"]


def _persist_pending_completion(event):
    from tools import async_delegation

    async_delegation._persist_dispatch({
        "delegation_id": event["delegation_id"],
        "session_key": event["session_key"],
        "origin_ui_session_id": "",
        "parent_session_id": event.get("parent_session_id"),
        "dispatched_at": event["dispatched_at"],
    })
    async_delegation._persist_completion(event, {
        "status": "completed",
        "summary": event["summary"],
    })


def test_explicit_kill_returns_output_before_consuming_notification(monkeypatch):
    import tools.process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_consumed",
        command="sleep 999",
        task_id="task",
        started_at=1.0,
        output_buffer="important terminal output\n",
        notify_on_complete=True,
    )
    session.process = MagicMock()
    session.process.pid = 4242
    registry._running[session.id] = session
    monkeypatch.setattr(registry, "_terminate_host_pid", lambda *_a, **_kw: None)
    monkeypatch.setattr(registry, "_write_checkpoint", lambda: None)
    monkeypatch.setattr(pr_module, "process_registry", registry)

    result = registry.kill_process(session.id)
    assert result["status"] == "killed"
    assert result["output"] == "important terminal output\n"
    assert registry.is_completion_consumed(session.id)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    adapter.handle_message.assert_not_awaited()


def test_process_tool_redacts_explicit_kill_output(monkeypatch):
    from tools import process_registry as pr_module

    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_kill_redacted",
        command="printenv",
        task_id="task",
        started_at=1.0,
        output_buffer="PRIVATE_TOKEN=opaque-value\n",
        exited=True,
        exit_code=0,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)

    def _redact(result):
        assert result["output"] == "PRIVATE_TOKEN=opaque-value\n"
        result["output"] = "PRIVATE_TOKEN=<redacted>\n"
        return result

    monkeypatch.setattr(pr_module, "_redact_process_result", _redact)

    result = json.loads(pr_module._handle_process({
        "action": "kill",
        "session_id": session.id,
    }))
    assert result["output"] == "PRIVATE_TOKEN=<redacted>\n"


def test_autonomous_completion_redacts_real_command_and_output_secrets(monkeypatch):
    import agent.redact as redact_module
    import tools.process_registry as pr_module

    secret = "abc123randomopaquetokenvalue999"
    registry = ProcessRegistry()
    session = ProcessSession(
        id="proc_autonomous_redaction",
        command=f"printenv MY_SERVICE_TOKEN={secret}",
        task_id="task",
        started_at=1234.5,
        output_buffer=f"MY_SERVICE_TOKEN={secret}\nHOME=/home/user\n",
        exited=True,
        exit_code=0,
        notify_on_complete=True,
    )
    registry._finished[session.id] = session
    monkeypatch.setattr(pr_module, "process_registry", registry)
    monkeypatch.setattr(redact_module, "_REDACT_ENABLED", True)

    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(adapter)

    async def _instant_sleep(*_a, **_kw):
        pass

    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    asyncio.run(runner._run_process_watcher({
        "session_id": session.id,
        "check_interval": 0,
        "session_key": "agent:main:telegram:dm:123",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "notify_on_complete": True,
    }))

    delivered = adapter.handle_message.await_args.args[0]
    assert secret not in delivered.text
    assert "HOME=/home/user" in delivered.text
