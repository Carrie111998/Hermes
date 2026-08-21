"""Security contract for exact-once delegated parent approvals."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock

import pytest

from agent.delegation_context import delegated_approval_context, get_delegated_approval_authority
from hermes_cli.config_defaults import DEFAULT_CONFIG
from tools import delegated_approval as da
from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _active_subagents,
    _active_subagents_lock,
    _handle_delegate_task,
    _run_single_child,
)
from tools.process_registry import format_process_notification


def _digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _authority(*, command: str = "python -c 'print(1)'", ui: bool = False):
    parent = object()
    child = object()
    transport = object() if ui else None
    record = object() if ui else None
    authority = da.DelegatedApprovalAuthority(
        owner_agent=parent,
        child_agent=child,
        subagent_id=f"subagent-{id(child)}",
        child_session_id=f"child-{id(child)}",
        parent_session_id=f"parent-{id(parent)}",
        owner_approval_session_key=f"route-{id(parent)}",
        owner_session_id=f"ui-{id(parent)}" if ui else None,
        owner_transport=transport,
        owner_session_record=record,
        delegation_id=f"deleg-{id(child)}",
        parent_lane_enabled=True,
        parent_task_id=f"task-{id(parent)}",
        delegated_goal="test delegated goal",
    )
    with _active_subagents_lock:
        _active_subagents[authority.subagent_id] = {
            "subagent_id": authority.subagent_id,
            "agent": child,
            "owner_agent": parent,
            "owner_session_id": authority.owner_session_id,
            "owner_transport": transport,
            "owner_session_record": record,
            "approval_authority": authority,
            "accepting_steer": True,
        }
    return parent, child, authority


def test_desktop_child_binds_live_owner_generation_from_parent_agent_identity():
    """A worker-retained UI id + parent object must recover the exact generation."""
    import tui_gateway.server as server
    from gateway.session_context import clear_session_vars, set_session_vars
    from tui_gateway.transport import bind_transport, reset_transport

    owner_transport = object()
    parent = MagicMock()
    parent.session_id = "parent-desktop"
    owner_record = {
        "agent": parent,
        "session_key": parent.session_id,
        "transport": owner_transport,
    }
    child = MagicMock()
    child._subagent_id = "subagent-desktop-owner"
    child._delegate_depth = 1
    child._parent_subagent_id = None
    child._delegated_approval_metadata = {
        "enabled": True,
        "session_key": parent.session_id,
        "delegation_id": "deleg-desktop-owner",
        "parent_task_id": "parent-task",
        "delegated_goal": "closed local expression",
    }
    child.session_id = "child-desktop"
    child.model = "test-model"
    observed = {}

    def run_conversation(**_kwargs):
        observed["authority"] = get_delegated_approval_authority()
        return {
            "final_response": "done",
            "completed": True,
            "interrupted": False,
            "api_calls": 1,
            "messages": [],
        }

    child.run_conversation.side_effect = run_conversation
    server._sessions["ui-desktop-owner"] = owner_record
    from tools.delegate_tool import _capture_gateway_steer_authority

    assert _capture_gateway_steer_authority(
        "ui-desktop-owner", object()
    ) == (None, None)
    session_tokens = set_session_vars(
        source="desktop",
        session_key=parent.session_id,
        session_id=parent.session_id,
        ui_session_id="ui-desktop-owner",
    )
    transport_token = bind_transport(None)
    try:
        _run_single_child(0, "closed local expression", child=child, parent_agent=parent)
    finally:
        reset_transport(transport_token)
        clear_session_vars(session_tokens)
        server._sessions.pop("ui-desktop-owner", None)

    authority = observed["authority"]
    assert isinstance(authority, da.DelegatedApprovalAuthority)
    assert authority.owner_agent is parent
    assert authority.child_agent is child
    assert authority.owner_session_id == "ui-desktop-owner"
    assert authority.owner_transport is owner_transport
    assert authority.owner_session_record is owner_record
    replacement = {
        "agent": parent,
        "session_key": parent.session_id,
        "transport": owner_transport,
    }
    server._sessions["ui-desktop-owner"] = replacement
    try:
        assert not server._session_generation_matches(
            "ui-desktop-owner", owner_transport, owner_record, parent
        )
    finally:
        server._sessions.pop("ui-desktop-owner", None)


@pytest.fixture(autouse=True)
def _clean_registry():
    from tools import approval

    da.revoke_all("test-start")
    with _active_subagents_lock:
        _active_subagents.clear()
    with approval._lock:
        saved_permanent = set(approval._permanent_approved)
        saved_session = {
            key: set(values) for key, values in approval._session_approved.items()
        }
        saved_yolo = set(approval._session_yolo)
        approval._permanent_approved.clear()
        approval._session_approved.clear()
        approval._session_yolo.clear()
    yield
    da.revoke_all("test-cleanup")
    with _active_subagents_lock:
        _active_subagents.clear()
    with approval._lock:
        approval._permanent_approved.clear()
        approval._permanent_approved.update(saved_permanent)
        approval._session_approved.clear()
        approval._session_approved.update(saved_session)
        approval._session_yolo.clear()
        approval._session_yolo.update(saved_yolo)


def _start_wait(authority, command: str, *, timeout: float = 2.0):
    outcome: dict = {}

    def wait_for_parent():
        with delegated_approval_context(authority):
            outcome.update(
                da.await_parent_decision(
                    command=command,
                    description="local test",
                    pattern_keys=["script execution via -e/-c flag"],
                    request_identity=da.capture_request_identity(
                        command, f"call-{id(authority.child_agent)}"
                    ),
                    timeout=timeout,
                )
            )

    thread = threading.Thread(target=wait_for_parent, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        pending = da.pending_requests()
        match = next(
            (
                request
                for request in pending
                if request["subagent_id"] == authority.subagent_id
            ),
            None,
        )
        if match:
            return thread, outcome, match["approval_id"]
        time.sleep(0.005)
    pytest.fail("approval request was not registered")


def _guard_on_existing_user_or_block_path(monkeypatch, command: str) -> tuple[dict, list, list]:
    """Exercise the real guard while making the existing user seam deterministic."""
    from tools import approval

    parent, _, authority = _authority(command=command)
    parent_events: list[dict] = []
    user_requests: list[dict] = []
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    monkeypatch.setattr(da, "_publish_parent_event", parent_events.append)

    def _deny_user(_session_key, _notify, approval_data, **_kwargs):
        user_requests.append(approval_data)
        return {"resolved": True, "choice": "deny"}

    monkeypatch.setattr(approval, "_await_gateway_decision", _deny_user)
    approval.register_gateway_notify(authority.owner_approval_session_key, lambda _data: None)
    session_token = approval.set_current_session_key(authority.owner_approval_session_key)
    tool_token = approval._approval_tool_call_id.set("call-negative-control")
    child_context = __import__("contextvars").copy_context()
    outcome: dict = {}

    def _run_guard():
        with delegated_approval_context(authority):
            outcome.update(approval.check_all_command_guards(command, "local"))

    try:
        thread = threading.Thread(target=lambda: child_context.run(_run_guard), daemon=True)
        thread.start()
        deadline = time.monotonic() + 1
        while thread.is_alive() and time.monotonic() < deadline:
            if parent_events:
                da.resolve_parent_decision(parent, parent_events[0]["approval_id"], "deny")
                break
            time.sleep(0.002)
        thread.join(1)
        assert not thread.is_alive(), "guard did not reach either deterministic decision seam"
    finally:
        approval._approval_tool_call_id.reset(tool_token)
        approval.reset_current_session_key(session_token)
        approval.unregister_gateway_notify(authority.owner_approval_session_key)
    return outcome, parent_events, user_requests


def test_authority_is_context_local_and_contains_no_resolver_capability():
    _, _, authority = _authority()
    assert get_delegated_approval_authority() is None
    with delegated_approval_context(authority):
        assert get_delegated_approval_authority() is authority
        assert not hasattr(authority, "resolve")
        assert not hasattr(authority, "approval_id")
    assert get_delegated_approval_authority() is None


def test_classifier_requires_structured_safe_lane_and_local_authority():
    command = "python -c 'print(1)'"
    _, _, authority = _authority(command=command)
    assert da.is_specialist_local_reversible(
        authority, command, "local", pattern_keys=["script execution via -e/-c flag"], tirith_findings=[]
    )
    assert not da.is_specialist_local_reversible(
        authority, command, "docker", pattern_keys=["script execution via -e/-c flag"], tirith_findings=[]
    )
    assert not da.is_specialist_local_reversible(
        authority, command, "local", pattern_keys=["tirith:remote-code"],
        tirith_findings=[{"rule_id": "remote-code", "severity": "HIGH"}],
    )
    assert not da.is_specialist_local_reversible(
        authority, command, "local", pattern_keys=["modify system permissions"], tirith_findings=[]
    )


@pytest.mark.parametrize(
    ("command", "env_type", "pattern_keys", "findings", "host_access"),
    [
        ("python -c 'print(1)'", "local", [], [], False),
        ("python -c 'print(1)'", "local", ["unknown structured key"], [], False),
        (
            "python -c 'print(1)'",
            "local",
            ["script execution via -e/-c flag", "modify system permissions"],
            [],
            False,
        ),
        (
            "python -c 'print(1)'",
            "local",
            ["script execution via -e/-c flag"],
            [{"rule_id": "secret-access", "severity": "HIGH"}],
            False,
        ),
        ("python -c 'print(1)'", "ssh", ["script execution via -e/-c flag"], [], False),
        ("python -c 'print(1)'", "local", ["script execution via -e/-c flag"], [], True),
        ("x" * 8193, "local", ["script execution via -e/-c flag"], [], False),
    ],
)
def test_classifier_uncertainty_and_owner_consequence_signals_fall_through(
    command, env_type, pattern_keys, findings, host_access
):
    _, _, authority = _authority()
    assert not da.is_specialist_local_reversible(
        authority,
        command,
        env_type,
        pattern_keys=pattern_keys,
        tirith_findings=findings,
        has_host_access=host_access,
    )


def test_classifier_accepts_only_exact_current_interpreter_aliases():
    command = "python -c 'print(1)'"
    _, _, authority = _authority()
    for key in (
        "script execution via -e/-c flag",
        "(python[23]?|perl|ruby|node)\\s+-[ec]\\s+",
    ):
        assert da.is_specialist_local_reversible(
            authority,
            command,
            "local",
            pattern_keys=[key],
            tirith_findings=[],
        )


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"import os; os.remove('/tmp/important')\"",
        "python -c \"from pathlib import Path; Path('/tmp/important').unlink()\"",
        "python -c \"open('/tmp/security-control','w').write('disabled')\"",
        "python -c \"import socket; socket.create_connection(('127.0.0.1', 22))\"",
        "python -c \"__import__('os').remove('/tmp/important')\"",
        "python -c \"import subprocess; subprocess.run(['id'])\"",
        "python -c \"import os; print(os.environ.get('TOKEN'))\"",
        "python -c \"print(open('/etc/shadow').read())\"",
        "python -c \"(lambda: 1)()\"",
        "python -c \"[x for x in [1]]\"",
        "python -c \"print(1)\"; sudo id",
        "python -c \"print(1)\" | sh",
        "python -c \"print(1)\" > /tmp/output",
        "sh -c \"python -c 'print(1)'\"",
        "docker run python -c \"print(1)\"",
        "ssh host python -c \"print(1)\"",
        "perl -e 'print 1'",
        "ruby -e 'puts 1'",
        "node -e 'console.log(1)'",
        "python -c \"if\"",
        "python -c \"print(1)\" extra",
    ],
)
def test_real_guard_keeps_unsafe_or_non_python_inline_code_on_existing_path(
    monkeypatch, command
):
    result, parent_events, user_requests = _guard_on_existing_user_or_block_path(
        monkeypatch, command
    )
    assert result["approved"] is False
    assert parent_events == []
    assert da.pending_requests() == []
    assert user_requests or result.get("hardline") or result.get("outcome") == "denied"


def test_strict_utf8_and_size_boundary_reject_before_parent_registration(monkeypatch):
    for command in ("python -c 'print(1)' # \ud800", "python -c '" + "x" * 8192 + "'"):
        result, parent_events, _user_requests = _guard_on_existing_user_or_block_path(
            monkeypatch, command
        )
        assert result["approved"] is False
        assert parent_events == []
        assert da.pending_requests() == []


@pytest.mark.parametrize(
    "command",
    [
        "python -c 'print(1)'",
        "python3 -c 'print(\"dynamic-\" + \"literal\")'",
        "python3.11 -c 'print((1 + 2) * 3)'",
    ],
)
def test_closed_python_subset_is_positive_eligibility_proof(command):
    _, _, authority = _authority(command=command)
    assert da.is_specialist_local_reversible(
        authority,
        command,
        "local",
        pattern_keys=["script execution via -e/-c flag"],
        tirith_findings=[],
    )


def test_matching_parent_resolves_exact_request_once(monkeypatch):
    command = "python -m pytest tests/safe.py"
    parent, _, authority = _authority(command=command)
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    thread, outcome, approval_id = _start_wait(authority, command)
    assert da.resolve_parent_decision(parent, approval_id, "once")["resolved"] is True
    assert da.resolve_parent_decision(parent, approval_id, "once")["resolved"] is False
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "once"}


def test_active_child_guard_pauses_parent_resolves_and_child_resumes_once(monkeypatch):
    from tools import approval

    command = "python -c 'print(1)'"
    parent, _, authority = _authority(command=command)
    published: list[dict] = []
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    monkeypatch.setattr(da, "_publish_parent_event", published.append)
    outcome: dict = {}
    executions: list[str] = []

    def child_guard():
        token = approval._approval_tool_call_id.set("call-e2e")
        try:
            with delegated_approval_context(authority):
                outcome.update(approval.check_all_command_guards(command, "local"))
                if outcome.get("approved"):
                    executions.append("final-result")
        finally:
            approval._approval_tool_call_id.reset(token)

    thread = threading.Thread(target=child_guard, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not published:
        time.sleep(0.005)
    assert published and thread.is_alive()
    approval_id = published[0]["approval_id"]
    assert da.resolve_parent_decision(parent, approval_id, "once")["resolved"]
    thread.join(1)
    assert outcome["approved"] is True
    assert outcome["parent_agent_approved"] is True
    assert executions == ["final-result"]
    assert not da.resolve_parent_decision(parent, approval_id, "once")["resolved"]


def test_dynamic_command_never_prelisted_reaches_parent_once_and_resumes(monkeypatch):
    """The parent attests the exact command only after the child creates it."""
    from tools import approval

    command = "python -c 'print(\"dynamic-child-value\")'"
    parent, _, authority = _authority(command="different preconfigured command")
    published: list[dict] = []
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    monkeypatch.setattr(da, "_publish_parent_event", published.append)
    outcome: dict = {}
    resumed: list[str] = []

    def child_guard():
        token = approval._approval_tool_call_id.set("call-dynamic")
        try:
            with delegated_approval_context(authority):
                outcome.update(approval.check_all_command_guards(command, "local"))
                if outcome.get("approved"):
                    resumed.append("one-final-child-result")
        finally:
            approval._approval_tool_call_id.reset(token)

    thread = threading.Thread(target=child_guard, daemon=True)
    thread.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and not published:
        time.sleep(0.005)

    assert published and thread.is_alive()
    event = published[0]
    assert event["command"] == command
    assert event["command_digest"] == _digest(command)
    assert "tool_call_id" not in event
    assert da.resolve_parent_decision(
        parent, event["approval_id"], "once"
    )["resolved"]
    thread.join(1)
    assert outcome["parent_agent_approved"] is True
    assert resumed == ["one-final-child-result"]


@pytest.mark.parametrize(
    ("parent_choice", "expected_user_calls", "expected_approved"),
    [("deny", 0, False), ("escalate_to_user", 1, True)],
)
def test_deny_never_prompts_user_but_escalate_explicitly_uses_existing_user_path(
    monkeypatch, parent_choice, expected_user_calls, expected_approved
):
    from tools import approval

    command = "python -c 'print(1)'"
    _, _, authority = _authority()
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(
        "tools.tirith_security.check_command_security",
        lambda _command: {"action": "allow", "findings": [], "summary": ""},
    )
    monkeypatch.setattr(
        da,
        "await_parent_decision",
        lambda **_kwargs: {"resolved": True, "choice": parent_choice},
    )
    user_calls: list[dict] = []

    def user_decision(_session_key, _notify, approval_data, **_kwargs):
        user_calls.append(approval_data)
        return {"resolved": True, "choice": "once"}

    monkeypatch.setattr(approval, "_await_gateway_decision", user_decision)
    approval.register_gateway_notify(authority.owner_approval_session_key, lambda _data: None)
    token = approval.set_current_session_key(authority.owner_approval_session_key)
    tool_token = approval._approval_tool_call_id.set("call-parent-choice")
    try:
        with delegated_approval_context(authority):
            result = approval.check_all_command_guards(command, "local")
    finally:
        approval._approval_tool_call_id.reset(tool_token)
        approval.reset_current_session_key(token)
        approval.unregister_gateway_notify(authority.owner_approval_session_key)

    assert len(user_calls) == expected_user_calls
    assert result["approved"] is expected_approved
    if parent_choice == "deny":
        assert result["outcome"] == "denied"
        assert result["user_consent"] is False
    else:
        assert result["user_approved"] is True


def test_ids_alone_cannot_authorize_self_sibling_or_other_parent(monkeypatch):
    command = "python -m pytest tests/safe.py"
    parent, child, authority = _authority(command=command)
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    thread, outcome, approval_id = _start_wait(authority, command)
    assert not da.resolve_parent_decision(child, approval_id, "once")["resolved"]
    assert not da.resolve_parent_decision(object(), approval_id, "once")["resolved"]
    assert da.resolve_parent_decision(parent, approval_id, "deny")["resolved"]
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "deny"}


def test_command_and_tool_call_binding_are_frozen_against_nonempty_substitution(monkeypatch):
    command = "python -m pytest tests/safe.py"
    parent, _, authority = _authority(command=command)
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    thread, outcome, approval_id = _start_wait(authority, command)
    with da._lock:
        entry = da._pending[approval_id]
        with pytest.raises(FrozenInstanceError):
            entry.binding.tool_call_id = entry.binding.tool_call_id[:-1] + "X"
        with pytest.raises(FrozenInstanceError):
            entry.binding.raw_command = command + " -x"
    assert da.resolve_parent_decision(parent, approval_id, "once")["resolved"]
    thread.join(1)
    assert outcome["choice"] == "once"


def test_concurrent_request_identity_mismatch_fails_before_publication(monkeypatch):
    _, _, authority = _authority()
    published: list[dict] = []
    monkeypatch.setattr(da, "_publish_parent_event", published.append)
    identity = da.capture_request_identity("python -c 'print(1)'", "call-A")
    with delegated_approval_context(authority):
        result = da.await_parent_decision(
            command="python -c 'print(2)'",
            description="mismatch",
            pattern_keys=["script execution via -e/-c flag"],
            request_identity=identity,
            timeout=0.01,
        )
    assert result.get("ineligible") is True
    assert published == []
    assert da.pending_requests() == []


def test_expiry_revocation_and_transport_generation_replacement_fail_closed(monkeypatch):
    command = "python -m pytest tests/safe.py"
    parent, _, authority = _authority(command=command, ui=True)
    monkeypatch.setattr(
        "tui_gateway.server._session_generation_matches",
        lambda sid, transport, record, owner: (
            sid == authority.owner_session_id
            and transport is authority.owner_transport
            and record is authority.owner_session_record
            and owner is authority.owner_agent
        ),
    )
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    thread, outcome, approval_id = _start_wait(authority, command, timeout=0.03)
    thread.join(1)
    assert outcome == {"resolved": False, "choice": None, "expired": True}
    assert not da.resolve_parent_decision(parent, approval_id, "once")["resolved"]

    thread, outcome, approval_id = _start_wait(authority, command)
    monkeypatch.setattr(
        "tui_gateway.server._session_generation_matches",
        lambda _sid, _transport, _record, _owner: False,
    )
    thread.join(1)
    assert not thread.is_alive()
    assert outcome["choice"] == "deny"
    assert not da.resolve_parent_decision(parent, approval_id, "once")["resolved"]


@pytest.mark.parametrize(
    ("revoker", "reason"),
    [
        (lambda authority: da.revoke_for_child(authority.child_agent), "child_completed"),
        (
            lambda authority: da.revoke_for_parent_session(
                authority.owner_approval_session_key
            ),
            "parent_reset",
        ),
        (lambda _authority: da.revoke_all(), "process_exit"),
    ],
)
def test_lifecycle_revocation_unblocks_child_as_deny(monkeypatch, revoker, reason):
    command = "python -c 'print(1)'"
    parent, _, authority = _authority()
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    audits = []
    monkeypatch.setattr(da, "_audit", lambda _entry, decision, _by, why: audits.append((decision, why)))
    thread, outcome, approval_id = _start_wait(authority, command)

    assert revoker(authority) == 1
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "deny"}
    assert not da.resolve_parent_decision(parent, approval_id, "once")["resolved"]
    assert audits[-1] == ("revoked", reason)


def test_production_unregister_completion_revokes_waiter(monkeypatch):
    from tools.delegate_tool import _unregister_subagent

    _, child, authority = _authority()
    monkeypatch.setattr(da, "_publish_parent_event", lambda _event: None)
    thread, outcome, _approval_id = _start_wait(authority, "python -c 'print(1)'")
    _unregister_subagent(authority.subagent_id, agent=child)
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "deny"}


def test_production_interrupt_revokes_waiter(monkeypatch):
    from tools.delegate_tool import interrupt_subagent

    _, _, authority = _authority()
    monkeypatch.setattr(da, "_publish_parent_event", lambda _event: None)
    monkeypatch.setattr("tools.delegate_tool.request_hard_interrupt", lambda *_args: True)
    thread, outcome, _approval_id = _start_wait(authority, "python -c 'print(1)'")
    assert interrupt_subagent(authority.subagent_id) is True
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "deny"}


def test_production_parent_reset_revokes_waiter(monkeypatch):
    from tools import approval

    _, _, authority = _authority()
    monkeypatch.setattr(da, "_publish_parent_event", lambda _event: None)
    thread, outcome, _approval_id = _start_wait(authority, "python -c 'print(1)'")
    approval.clear_session(authority.owner_approval_session_key)
    thread.join(1)
    assert outcome == {"resolved": True, "choice": "deny"}


def test_concurrent_children_are_keyed_not_fifo(monkeypatch):
    command_a = "python -m pytest tests/a.py"
    command_b = "python -m pytest tests/b.py"
    parent_a, _, authority_a = _authority(command=command_a)
    parent_b, _, authority_b = _authority(command=command_b)
    monkeypatch.setattr(da, "_publish_parent_event", lambda event: None)
    thread_a, outcome_a, id_a = _start_wait(authority_a, command_a)
    thread_b, outcome_b, id_b = _start_wait(authority_b, command_b)
    assert da.resolve_parent_decision(parent_b, id_b, "once")["resolved"]
    assert da.resolve_parent_decision(parent_a, id_a, "deny")["resolved"]
    thread_a.join(1)
    thread_b.join(1)
    assert outcome_a["choice"] == "deny"
    assert outcome_b["choice"] == "once"


def test_resolver_runtime_and_schema_forbid_spawn_fields():
    params = DELEGATE_TASK_SCHEMA["parameters"]
    assert params["properties"]["approval_response"]["additionalProperties"] is False
    forbidden = params["allOf"][0]["then"]["not"]["anyOf"]
    assert {next(iter(item["required"])) for item in forbidden} >= {"goal", "tasks", "background"}
    result = _handle_delegate_task(
        {"approval_response": {"approval_id": "x", "choice": "once"}, "goal": "spawn"},
        parent_agent=object(),
    )
    assert "unavailable" in result.lower()
    assert _handle_delegate_task(
        {"approval_response": {"approval_id": "x", "choice": "session"}},
        parent_agent=object(),
    ).lower().find("unavailable") >= 0


def test_child_schema_never_advertises_parent_resolver_operation():
    from copy import deepcopy
    from types import SimpleNamespace
    from tools.delegate_tool import _strip_parent_resolver_from_child_tools

    child = SimpleNamespace(
        tools=[{"type": "function", "function": deepcopy(DELEGATE_TASK_SCHEMA)}]
    )
    _strip_parent_resolver_from_child_tools(child)
    delegate_schema = child.tools[0]["function"]
    assert "approval_response" not in delegate_schema["parameters"]["properties"]
    assert "allOf" not in delegate_schema["parameters"]
    assert "goal" in delegate_schema["parameters"]["properties"]


def test_default_policy_is_off_without_static_command_prelisting():
    policy = DEFAULT_CONFIG["approvals"]["delegated_parent"]
    assert policy == {"enabled": False}


def test_parent_event_is_bounded_redacted_system_authored_and_not_raw_secret():
    secret = "sk-" + "a" * 40
    evt = {
        "type": "delegated_approval_request",
        "approval_id": "opaque",
        "subagent_id": "sub",
        "child_session_id": "child",
        "command": "python -c 'x' " + secret + " z" * 1000,
        "description": "untrusted " + secret,
        "command_digest": "d" * 64,
        "pattern_keys": ["script execution via -e/-c flag"],
        "expires_in_seconds": 90,
    }
    formatted = format_process_notification(evt)
    assert formatted.startswith("[SYSTEM EVENT: delegated_approval_request]")
    assert "UNTRUSTED DATA" in formatted
    assert secret not in formatted
    payload = json.loads(formatted.splitlines()[2])
    assert len(payload["command"]) <= 600
    assert payload["untrusted_data"] is True
    assert payload["pattern_keys"] == ["script execution via -e/-c flag"]
    assert payload["expires_in_seconds"] == 90
    assert "tool_call_id" not in payload
    assert "parent_task_id" in payload
    assert "delegated_goal" in payload


def test_million_character_tool_call_id_fails_closed_before_publication(monkeypatch):
    _, _, authority = _authority()
    published: list[dict] = []
    monkeypatch.setattr(da, "_publish_parent_event", published.append)
    identity = da.capture_request_identity("python -c 'print(1)'", "x" * 1_000_000)
    with delegated_approval_context(authority):
        result = da.await_parent_decision(
            command="python -c 'print(1)'",
            description="bounded",
            pattern_keys=["script execution via -e/-c flag"],
            request_identity=identity,
            timeout=0.01,
        )
    assert result.get("ineligible") is True
    assert published == []
    assert da.pending_requests() == []


def test_formatter_rebounds_all_variable_fields_and_enforces_aggregate_limit():
    huge = "z" * 1_000_000
    evt = {
        "type": "delegated_approval_request",
        "approval_id": huge,
        "delegation_id": huge,
        "subagent_id": huge,
        "child_session_id": huge,
        "parent_session_id": huge,
        "session_key": huge,
        "origin_ui_session_id": huge,
        "command": huge,
        "description": huge,
        "command_digest": huge,
        "tool_call_id": huge,
        "pattern_keys": [huge] * 100,
        "parent_task_id": huge,
        "delegated_goal": huge,
        "expires_in_seconds": huge,
    }
    formatted = format_process_notification(evt)
    assert formatted.startswith("[SYSTEM EVENT: delegated_approval_request]")
    assert len(formatted.encode("utf-8")) <= da.MAX_SERIALIZED_MESSAGE_BYTES
    assert huge not in formatted


def test_formatter_rejects_invalid_utf8_surrogates_without_persisting_them():
    evt = {
        "type": "delegated_approval_request",
        "approval_id": "approval-\ud800",
        "delegation_id": "delegation",
        "subagent_id": "child",
        "child_session_id": "session",
        "parent_session_id": "parent",
        "session_key": "key",
        "origin_ui_session_id": "ui",
        "command": "python -c 'print(1)'\ud800",
        "description": "description\ud800",
        "command_digest": "digest",
        "pattern_keys": ["safe\ud800"],
        "parent_task_id": "task",
        "delegated_goal": "goal",
    }
    formatted = format_process_notification(evt)
    assert "\ud800" not in formatted
    assert len(formatted.encode("utf-8")) <= da.MAX_SERIALIZED_MESSAGE_BYTES


def test_audit_contains_digest_ids_and_no_raw_secret(monkeypatch):
    command = "python -c 'print(1)' # sk-" + "b" * 40
    parent, _, authority = _authority(command=command)
    captured = []
    monkeypatch.setattr("tools.approval._fire_approval_hook", lambda hook, **payload: captured.append((hook, payload)))
    identity = da.capture_request_identity(command, "call")
    entry = da._PendingApproval(
        approval_id="opaque",
        binding=da._ApprovalBinding(
            authority=authority,
            raw_command=command,
            command_digest=_digest(command),
            tool_call_id="call",
            request_identity=identity,
            description="contains sk-" + "b" * 40,
            pattern_keys=("execute Python code",),
        ),
        created_monotonic=1.0,
        expires_monotonic=2.0,
    )
    da._audit(entry, "requested", "system", "eligible")
    dumped = json.dumps(captured)
    assert command not in dumped
    assert "b" * 40 not in dumped
    assert _digest(command) in dumped
    assert authority.subagent_id in dumped
