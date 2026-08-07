"""Exact, non-semantic execution authority for delegated workers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import approval
from tools import code_execution_tool
from tools import terminal_tool


OWNER_ID = "owner-stage-b"
SESSION_KEY = "stage-b-delegated-session"


@pytest.fixture(autouse=True)
def _local_exact_authority(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "approvals:\n  plan_owner_user_ids:\n    - owner-stage-b\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(approval, "_writer_boundary_policy_required", lambda: False)
    monkeypatch.setattr(approval, "_canonical_brain_required", lambda: False)
    monkeypatch.setattr(approval, "_observed_session_user_id", lambda: "")
    monkeypatch.setattr(approval, "_observed_session_platform", lambda: "")
    monkeypatch.setattr(approval, "_observed_session_message_id", lambda: "")
    approval.clear_session_local(SESSION_KEY)
    yield hermes_home
    approval.clear_session_local(SESSION_KEY)


def _bind_delegated_session():
    session_token = approval.set_current_session_key(SESSION_KEY)
    delegated_token = approval.bind_delegated_exact_plan_consumer()
    return session_token, delegated_token


def _reset_delegated_session(session_token, delegated_token) -> None:
    approval.reset_delegated_exact_plan_consumer(delegated_token)
    approval.reset_current_session_key(session_token)


def test_isolated_worker_is_approval_free_without_content_inspection(monkeypatch):
    assert not hasattr(approval, "detect_hardline_command")
    assert not hasattr(approval, "detect_dangerous_command")
    assert not hasattr(approval, "_match_user_deny_rule")
    session_token, delegated_token = _bind_delegated_session()
    try:
        terminal_result = approval.check_all_command_guards(
            "opaque bytes; no classifier may inspect them",
            "isolated_worker",
        )
        code_result = approval.check_execute_code_guard(
            "opaque python bytes",
            "isolated_worker",
        )
    finally:
        _reset_delegated_session(session_token, delegated_token)

    assert terminal_result == {"approved": True, "message": None}
    assert code_result == {"approved": True, "message": None}


def test_nonisolated_child_requires_exact_typed_capabilities_before_semantics(
    monkeypatch,
):
    command = "printf 'terminal-authorized'"
    code = "print('code-authorized')\n"
    approval.grant_plan_capability(
        session_key=SESSION_KEY,
        plan_id="plan-stage-b",
        exact_commands=[command],
        exact_code_scripts=[code],
        approved_by_user_id=OWNER_ID,
        max_uses_per_command=1,
    )

    assert not hasattr(approval, "detect_hardline_command")
    assert not hasattr(approval, "detect_dangerous_command")
    assert not hasattr(approval, "_match_user_deny_rule")

    session_token, delegated_token = _bind_delegated_session()
    try:
        terminal_mismatch = approval.check_all_command_guards(
            "printf 'terminal-unauthorized'",
            "local",
        )
        code_mismatch = approval.check_execute_code_guard(
            code + "# changed\n",
            "local",
        )
        terminal_allowed = approval.check_all_command_guards(command, "local")
        code_allowed = approval.check_execute_code_guard(code, "local")
    finally:
        _reset_delegated_session(session_token, delegated_token)

    assert terminal_allowed["plan_capability"] == "plan-stage-b"
    assert code_allowed["plan_capability"] == "plan-stage-b"
    assert terminal_mismatch["outcome"] == "exact_plan_capability_required"
    assert code_mismatch["outcome"] == "exact_plan_capability_required"


def test_temp_root_real_terminal_and_execute_code_paths_use_exact_plan(
    monkeypatch,
    tmp_path,
):
    command = "pwd"
    code = "print('stage-b-code-ok')\n"
    config = {
        "env_type": "local",
        "cwd": str(tmp_path),
        "timeout": 30,
        "lifetime_seconds": 60,
        "local_persistent": False,
        "host_cwd": str(tmp_path),
    }
    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: dict(config))
    monkeypatch.setattr(terminal_tool, "_active_environments", {})
    monkeypatch.setattr(terminal_tool, "_last_activity", {})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(terminal_tool, "_session_cwd", {})
    approval.grant_plan_capability(
        session_key=SESSION_KEY,
        plan_id="plan-stage-b-e2e",
        exact_commands=[command],
        exact_code_scripts=[code],
        approved_by_user_id=OWNER_ID,
        max_uses_per_command=1,
    )

    session_token, delegated_token = _bind_delegated_session()
    try:
        terminal_payload = json.loads(
            terminal_tool.terminal_tool(
                command=command,
                task_id=SESSION_KEY,
                workdir=str(tmp_path),
            )
        )
        code_payload = json.loads(
            code_execution_tool.execute_code(
                code,
                task_id=SESSION_KEY,
                enabled_tools=[],
            )
        )
        blocked_payload = json.loads(
            terminal_tool.terminal_tool(
                command="pwd -P",
                task_id=SESSION_KEY,
                workdir=str(tmp_path),
            )
        )
    finally:
        _reset_delegated_session(session_token, delegated_token)

    assert terminal_payload["exit_code"] == 0
    assert Path(terminal_payload["output"].strip()) == tmp_path
    assert code_payload["status"] == "success"
    assert "stage-b-code-ok" in code_payload["output"]
    assert blocked_payload["status"] == "blocked"
    error = blocked_payload["error"]
    assert "exact requester-authorized plan capability" in error
    assert "owner-approved" not in error
