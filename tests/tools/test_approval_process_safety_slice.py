"""Focused regression tests for the approval/process-safety hardening slice."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tools import approval
from tools.process_registry import ProcessRegistry


def _headless_approval(monkeypatch):
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "ask")
    monkeypatch.setattr(approval, "_command_matches_permanent_allowlist", lambda command: False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)


def test_background_guard_cannot_bypass_dangerous_command(monkeypatch):
    _headless_approval(monkeypatch)

    result = approval.check_all_command_guards(
        "rm -rf /tmp/approval-slice-test", "local", operation_id="op-background"
    )

    assert result["approved"] is False
    assert result["outcome"] is approval.ApprovalOutcome.NON_INTERACTIVE
    assert result["retryable"] is False
    assert result["operation_id"] == "op-background"


def test_execute_code_background_guard_cannot_bypass_approval(monkeypatch):
    _headless_approval(monkeypatch)

    result = approval.check_execute_code_guard(
        "import subprocess; subprocess.run(['rm', '-rf', '/tmp/x'])",
        "local",
        operation_id="op-code-background",
    )

    assert result["approved"] is False
    assert result["outcome"] is approval.ApprovalOutcome.NON_INTERACTIVE
    assert result["retryable"] is False


def test_retry_boundary_is_immutable_and_operation_scoped():
    boundary = approval.ApprovalBoundary.for_request(
        "rm -rf /tmp/x", "local", operation_id="op-fixed"
    )

    with pytest.raises(FrozenInstanceError):
        boundary.operation_id = "op-mutated"
    assert boundary.operation_id == "op-fixed"


def test_non_retryable_cron_policy_block_is_typed(monkeypatch):
    _headless_approval(monkeypatch)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    result = approval.check_dangerous_command(
        "rm -rf /tmp/approval-slice-test", "local", operation_id="op-policy"
    )

    assert result["approved"] is False
    assert result["outcome"] is approval.ApprovalOutcome.POLICY_DENIED
    assert result["retryable"] is False


def test_timeout_and_explicit_denial_remain_distinct(monkeypatch):
    _headless_approval(monkeypatch)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)

    timeout = approval.check_dangerous_command(
        "rm -rf /tmp/approval-slice-test", "local",
        approval_callback=lambda *args, **kwargs: "timeout",
        operation_id="op-timeout",
    )
    denied = approval.check_dangerous_command(
        "rm -rf /tmp/approval-slice-test", "local",
        approval_callback=lambda *args, **kwargs: "deny",
        operation_id="op-denied",
    )

    assert timeout["approved"] is False
    assert timeout["outcome"] is approval.ApprovalOutcome.TIMEOUT
    assert denied["approved"] is False
    assert denied["outcome"] is approval.ApprovalOutcome.DENIED
    assert timeout["outcome"] != denied["outcome"]
    assert timeout["retryable"] is False
    assert denied["retryable"] is False


def test_identity_mismatch_never_reaches_tree_terminator(monkeypatch):
    called = []
    monkeypatch.setattr(ProcessRegistry, "_is_host_pid_alive", classmethod(lambda cls, pid: True))
    monkeypatch.setattr(ProcessRegistry, "_safe_host_start_time", staticmethod(lambda pid: 99))
    monkeypatch.setattr(
        ProcessRegistry,
        "_terminate_host_pid_legacy",
        classmethod(lambda cls, pid, expected_start=None: called.append(pid)),
    )

    evidence = ProcessRegistry._terminate_host_pid(12345, 11)

    assert evidence.status == "identity_mismatch"
    assert evidence.identity_verified is False
    assert called == []


def test_identity_verified_tree_reports_survivors(monkeypatch):
    import psutil

    class Child:
        pid = 222

    class Parent:
        def children(self, recursive=False):
            assert recursive is True
            return [Child()]

    monkeypatch.setattr(ProcessRegistry, "_host_pid_is_ours", classmethod(lambda cls, pid, start: True))
    monkeypatch.setattr(
        ProcessRegistry,
        "_safe_host_start_time",
        staticmethod(lambda pid: {111: 10, 222: 20}.get(pid)),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: Parent())
    monkeypatch.setattr(ProcessRegistry, "_terminate_host_pid_legacy", classmethod(lambda cls, pid, start: None))

    evidence = ProcessRegistry._terminate_host_pid(111, 10)

    assert evidence.identity_verified is True
    assert evidence.targeted_pids == (111, 222)
    assert evidence.survivors == (111, 222)
    assert evidence.status == "partial"
