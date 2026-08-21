"""Desktop async delegated-parent approval integration contracts."""
from __future__ import annotations

import json
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from tools import approval
from tools import async_delegation
from tools import delegate_tool
from tools import delegated_approval
from tools.process_registry import process_registry


COMMAND = "python3 -c 'print(6 * 7)'"
UI_SESSION_ID = "desktop-owner-live"


class _Transport:
    def write(self, _obj):
        return True


class _ParentAgent:
    session_id = "desktop-parent-session"
    provider = "test-provider"
    model = "test-model"
    _delegate_depth = 0
    _current_task_id = "desktop-parent-task"
    _active_children = []
    _active_children_lock = threading.RLock()
    _interrupt_requested = False


class _ChildAgent:
    session_id = "desktop-child-session"
    model = "test-model"
    _subagent_id = "desktop-child-live"
    _delegate_depth = 1
    _parent_subagent_id = None
    _interrupt_requested = False

    def __init__(self, fake_executor):
        self.fake_executor = fake_executor
        self.run_count = 0
        self.worker_thread_ids = []
        self.observed_authority = None

    def run_conversation(self, **_kwargs):
        from agent.delegation_context import get_delegated_approval_authority

        self.run_count += 1
        self.worker_thread_ids.append(threading.get_ident())
        self.observed_authority = get_delegated_approval_authority()
        tool_token = approval._approval_tool_call_id.set("desktop-tool-call-live")
        try:
            guard = approval.check_all_command_guards(COMMAND, "local")
        finally:
            approval._approval_tool_call_id.reset(tool_token)
        if guard.get("approved"):
            self.fake_executor(COMMAND)
        self.worker_thread_ids.append(threading.get_ident())
        return {
            "final_response": json.dumps(guard, sort_keys=True),
            "completed": True,
            "interrupted": False,
            "api_calls": 0,
            "messages": [],
        }

    def get_activity_summary(self):
        return {"api_call_count": 0, "current_tool": None, "last_activity_ts": 1.0}

    def interrupt(self):
        self._interrupt_requested = True


@pytest.fixture
def desktop_async_harness(monkeypatch):
    import tui_gateway.server as server
    from gateway.session_context import clear_session_vars, set_session_vars
    from tui_gateway.transport import bind_transport, current_transport, reset_transport

    parent = _ParentAgent()
    executions = []
    child = _ChildAgent(lambda command: executions.append(command))
    transport = _Transport()
    session = {
        "agent": parent,
        "session_key": parent.session_id,
        "transport": transport,
    }
    event_queue = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", event_queue)
    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", lambda **_kw: child)
    monkeypatch.setattr(
        delegate_tool,
        "_preflight_delegation_capabilities",
        lambda task_list, **_kw: ({index: None for index in range(len(task_list))}, {index: "test-model" for index in range(len(task_list))}),
    )
    monkeypatch.setattr(
        delegate_tool,
        "_resolve_delegation_credentials",
        lambda *_args, **_kwargs: {
            "model": "test-model",
            "provider": "test-provider",
            "base_url": "http://invalid.test",
            "api_key": "not-a-credential",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"max_iterations": 1})
    monkeypatch.setattr(delegate_tool, "_get_max_async_children", lambda: 4)
    monkeypatch.setattr(delegate_tool, "_get_child_timeout", lambda: 1.0)
    monkeypatch.setattr(
        delegate_tool,
        "_finalize_child_results",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        approval,
        "detect_dangerous_command",
        lambda _command: (False, None, None),
    )
    real_await = delegated_approval.await_parent_decision

    def bounded_await(**kwargs):
        kwargs["timeout"] = 0.15
        return real_await(**kwargs)

    monkeypatch.setattr(delegated_approval, "await_parent_decision", bounded_await)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"approvals": {"delegated_parent": {"enabled": True}}},
    )
    server._sessions[UI_SESSION_ID] = session
    session_tokens = set_session_vars(
        source="tui",
        session_key=parent.session_id,
        session_id=parent.session_id,
        ui_session_id=UI_SESSION_ID,
        async_delivery=True,
    )
    # Deliberately shed request transport authority before delegate_task enters
    # the real background-dispatch seam. The exact parent object + live Desktop
    # session generation are the only remaining authority source.
    transport_token = bind_transport(None)
    assert current_transport() is None
    try:
        yield {
            "parent": parent,
            "child": child,
            "transport": transport,
            "session": session,
            "events": event_queue,
            "executions": executions,
            "server": server,
        }
    finally:
        reset_transport(transport_token)
        clear_session_vars(session_tokens)
        server._sessions.pop(UI_SESSION_ID, None)
        delegated_approval.revoke_all("desktop-async-test-cleanup")
        with delegate_tool._active_subagents_lock:
            delegate_tool._active_subagents.clear()
        with async_delegation._records_lock:
            async_delegation._records.clear()


def _dispatch(harness):
    payload = json.loads(
        delegate_tool._handle_delegate_task(
            {"goal": "closed local expression"},
            parent_agent=harness["parent"],
        )
    )
    assert payload["status"] == "dispatched"
    return payload["delegation_id"]


def _next_approval_event(harness, timeout=1.0):
    deadline = time.monotonic() + timeout
    deferred = []
    try:
        while time.monotonic() < deadline:
            try:
                event = harness["events"].get(
                    timeout=max(0.01, deadline - time.monotonic())
                )
            except queue.Empty:
                break
            if event.get("type") == "delegated_approval_request":
                return event
            deferred.append(event)
    finally:
        for event in deferred:
            harness["events"].put(event)
    pytest.fail(
        "exact delegated approval request was not emitted; "
        f"authority={harness['child'].observed_authority!r}; events={deferred!r}"
    )


def _wait_for_completion(harness, delegation_id, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = harness["events"].get(timeout=max(0.01, deadline - time.monotonic()))
        if event.get("type") == "async_delegation" and event.get("delegation_id") == delegation_id:
            return event
    pytest.fail("async delegated child did not close within the deterministic bound")


@pytest.mark.parametrize(
    "authority_kind",
    ["serialized", "fake", "disabled", "getter_exception"],
)
def test_noninteractive_authority_bypass_rejects_untrusted_context(
    monkeypatch, authority_kind
):
    from agent import delegation_context

    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda _c: False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: pytest.fail("untrusted authority bypassed the legacy return"),
    )
    monkeypatch.setattr(
        approval,
        "detect_dangerous_command",
        lambda _command: pytest.fail("untrusted authority reached external guard work"),
    )

    if authority_kind == "serialized":
        authority = {"parent_lane_enabled": True}
    elif authority_kind == "fake":
        authority = SimpleNamespace(parent_lane_enabled=True)
    else:
        authority = delegated_approval.DelegatedApprovalAuthority(
            owner_agent=object(),
            child_agent=object(),
            subagent_id="disabled-child",
            child_session_id="disabled-child-session",
            parent_session_id="disabled-parent-session",
            owner_approval_session_key="disabled-owner-key",
            owner_session_id=None,
            owner_transport=None,
            owner_session_record=None,
            delegation_id="disabled-delegation",
            parent_lane_enabled=False,
        )

    if authority_kind == "getter_exception":
        monkeypatch.setattr(
            delegation_context,
            "get_delegated_approval_authority",
            lambda: (_ for _ in ()).throw(RuntimeError("closed lookup failure")),
        )

    with delegation_context.delegated_approval_context(authority):
        assert approval.check_all_command_guards("echo safe", "local") == {
            "approved": True,
            "message": None,
        }


def test_desktop_async_worker_exact_owner_once_resumes_same_child_once(desktop_async_harness):
    harness = desktop_async_harness
    delegation_id = _dispatch(harness)
    event = _next_approval_event(harness)

    assert event == {
        "type": "delegated_approval_request",
        "approval_id": event["approval_id"],
        "delegation_id": delegation_id,
        "subagent_id": harness["child"]._subagent_id,
        "child_session_id": harness["child"].session_id,
        "parent_session_id": harness["parent"].session_id,
        "session_key": harness["parent"].session_id,
        "origin_ui_session_id": UI_SESSION_ID,
        "command": COMMAND,
        "description": "inline interpreter execution requires review",
        "command_digest": event["command_digest"],
        "pattern_keys": ["script execution via -e/-c flag"],
        "choices": ["once", "deny", "escalate_to_user"],
        "expires_in_seconds": 0.15,
        "untrusted_data": True,
        "system_authored": True,
        "parent_task_id": harness["parent"]._current_task_id,
        "delegated_goal": "closed local expression",
    }
    impostor = _ParentAgent()
    impostor.session_id = harness["parent"].session_id
    assert delegated_approval.resolve_parent_decision(
        impostor, event["approval_id"], "once"
    ) == {"resolved": False, "status": "unavailable"}
    assert harness["executions"] == []

    assert delegated_approval.resolve_parent_decision(
        harness["parent"], event["approval_id"], "once"
    ) == {"resolved": True, "choice": "once"}
    completion = _wait_for_completion(harness, delegation_id)

    assert completion["status"] == "completed"
    assert harness["child"].run_count == 1
    assert len(set(harness["child"].worker_thread_ids)) == 1
    assert harness["executions"] == [COMMAND]
    assert delegated_approval.resolve_parent_decision(
        harness["parent"], event["approval_id"], "once"
    ) == {"resolved": False, "status": "unavailable"}


@pytest.mark.parametrize(
    "replacement",
    ["no_decision", "session_record", "transport", "agent"],
)
def test_desktop_async_worker_missing_or_replaced_owner_fails_closed(
    desktop_async_harness, replacement
):
    harness = desktop_async_harness
    delegation_id = _dispatch(harness)
    event = _next_approval_event(harness)

    if replacement == "session_record":
        harness["server"]._sessions[UI_SESSION_ID] = {
            "agent": harness["parent"],
            "session_key": harness["parent"].session_id,
            "transport": harness["transport"],
        }
    elif replacement == "transport":
        harness["session"]["transport"] = _Transport()
    elif replacement == "agent":
        replacement_agent = _ParentAgent()
        replacement_agent.session_id = harness["parent"].session_id
        harness["session"]["agent"] = replacement_agent

    if replacement != "no_decision":
        assert delegated_approval.resolve_parent_decision(
            harness["parent"], event["approval_id"], "once"
        ) == {"resolved": False, "status": "unavailable"}

    started = time.monotonic()
    completion = _wait_for_completion(harness, delegation_id)
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert completion["status"] == "completed"
    assert harness["child"].run_count == 1
    assert harness["executions"] == []
    assert delegated_approval.resolve_parent_decision(
        harness["parent"], event["approval_id"], "once"
    ) == {"resolved": False, "status": "unavailable"}
