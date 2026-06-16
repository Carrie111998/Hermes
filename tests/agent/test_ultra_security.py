import json
from pathlib import Path

from agent.ultra_security import (
    DecisionLog,
    PolicyChecker,
    Principal,
    SandboxLease,
    ToolRequest,
    authorize_tool_call,
    issue_sandbox_lease,
    reset_current_principal,
    reset_current_sandbox_lease,
    set_current_principal,
    set_current_sandbox_lease,
)
from hermes_cli.middleware import run_tool_execution_middleware


def _owner() -> Principal:
    return Principal(
        tenant_id="ten_1",
        workspace_id="ws_1",
        project_id="proj_1",
        user_id="user_1",
        roles=("owner",),
        session_id="sess_1",
    )


def _viewer() -> Principal:
    return Principal(
        tenant_id="ten_1",
        workspace_id="ws_1",
        project_id="proj_1",
        user_id="user_2",
        roles=("viewer",),
        session_id="sess_1",
    )


def _lease(principal: Principal | None = None) -> SandboxLease:
    return issue_sandbox_lease(principal or _owner(), sandbox_id="sbx_1")


def _decision_log_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "logs" / "security_decisions.jsonl"


def _read_decisions() -> list[dict]:
    path = _decision_log_path()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_policy_checker_denies_missing_principal_for_high_risk_tool():
    request = ToolRequest(
        tool_name="terminal",
        args={"command": "pwd"},
        session_id="sess_1",
        tool_call_id="tc_1",
    )

    decision = PolicyChecker().authorize(None, request)

    assert decision.allowed is False
    assert decision.reason == "missing_principal"
    assert decision.risk == "high"
    assert decision.tool_call_id == "tc_1"


def test_authorize_tool_call_writes_append_only_decision_without_arg_values():
    decision = authorize_tool_call(
        "terminal",
        {"command": "echo TOP_SECRET", "api_key": "SHOULD_NOT_BE_LOGGED"},
        task_id="task_1",
        session_id="sess_1",
        tool_call_id="tc_1",
        principal=_owner(),
        sandbox_lease=_lease(),
        decision_log=DecisionLog(_decision_log_path()),
    )

    records = _read_decisions()
    assert decision.allowed is True
    assert records[-1]["decision_id"] == decision.decision_id
    assert records[-1]["tenant_id"] == "ten_1"
    assert records[-1]["workspace_id"] == "ws_1"
    assert records[-1]["project_id"] == "proj_1"
    assert records[-1]["user_id"] == "user_1"
    assert records[-1]["arg_keys"] == ["api_key", "command"]
    log_text = _decision_log_path().read_text(encoding="utf-8")
    assert "TOP_SECRET" not in log_text
    assert "SHOULD_NOT_BE_LOGGED" not in log_text


def test_tool_execution_middleware_blocks_before_next_call():
    called = False

    def _mark_called():
        nonlocal called
        called = True
        return "SHOULD_NOT_RUN"

    token = set_current_principal(_viewer())
    try:
        result = run_tool_execution_middleware(
            "terminal",
            {"command": "pwd"},
            lambda _args: _mark_called(),
            task_id="task_1",
            session_id="sess_1",
            tool_call_id="tc_block",
        )
    finally:
        reset_current_principal(token)

    payload = json.loads(result)
    records = _read_decisions()
    assert called is False
    assert payload["error_type"] == "policy_denied"
    assert payload["reason"] == "insufficient_role"
    assert payload["decision_id"] == records[-1]["decision_id"]
    assert records[-1]["allowed"] is False
    assert records[-1]["tool_name"] == "terminal"


def test_tool_execution_middleware_allows_owner_and_logs_decision():
    observed = []
    principal = _owner()
    principal_token = set_current_principal(principal)
    lease_token = set_current_sandbox_lease(_lease(principal))
    try:
        result = run_tool_execution_middleware(
            "terminal",
            {"command": "pwd"},
            lambda args: observed.append(args) or "OK",
            task_id="task_1",
            session_id="sess_1",
            tool_call_id="tc_allow",
        )
    finally:
        reset_current_sandbox_lease(lease_token)
        reset_current_principal(principal_token)

    records = _read_decisions()
    assert result == "OK"
    assert observed == [{"command": "pwd"}]
    assert records[-1]["allowed"] is True
    assert records[-1]["reason"] == "allowed"
    assert records[-1]["tool_call_id"] == "tc_allow"


def test_model_tools_dispatch_is_blocked_by_policy_before_registry(monkeypatch):
    import model_tools
    from tools.registry import registry

    called = False

    def _dispatch(_name, _args, **_kwargs):
        nonlocal called
        called = True
        return "SHOULD_NOT_RUN"

    monkeypatch.setattr(registry, "dispatch", _dispatch)
    token = set_current_principal(_viewer())
    try:
        result = model_tools.handle_function_call(
            "terminal",
            {"command": "pwd"},
            task_id="task_1",
            session_id="sess_1",
            tool_call_id="tc_model_tools",
            skip_pre_tool_call_hook=True,
        )
    finally:
        reset_current_principal(token)

    payload = json.loads(result)
    assert called is False
    assert payload["error_type"] == "policy_denied"
    assert payload["reason"] == "insufficient_role"
    assert _read_decisions()[-1]["tool_call_id"] == "tc_model_tools"


def test_policy_checker_denies_sandbox_tool_without_lease_for_scoped_principal():
    request = ToolRequest(
        tool_name="terminal",
        args={"command": "pwd"},
        session_id="sess_1",
        tool_call_id="tc_no_lease",
    )

    decision = PolicyChecker().authorize(_owner(), request)

    assert decision.allowed is False
    assert decision.reason == "missing_sandbox_lease"


def test_policy_checker_denies_mismatched_sandbox_lease():
    owner = _owner()
    other = Principal(
        tenant_id="ten_1",
        workspace_id="ws_1",
        project_id="proj_1",
        user_id="other_user",
        roles=("owner",),
        session_id="sess_1",
    )
    request = ToolRequest(
        tool_name="terminal",
        args={"command": "pwd"},
        session_id="sess_1",
        tool_call_id="tc_bad_lease",
    )

    decision = PolicyChecker().authorize(owner, request, _lease(other))

    assert decision.allowed is False
    assert decision.reason == "sandbox_lease_mismatch"
    assert decision.sandbox_id == "sbx_1"


def test_tool_execution_middleware_allows_owner_with_matching_lease():
    observed = []
    principal = _owner()
    principal_token = set_current_principal(principal)
    lease_token = set_current_sandbox_lease(_lease(principal))
    try:
        result = run_tool_execution_middleware(
            "terminal",
            {"command": "pwd"},
            lambda args: observed.append(args) or "OK",
            task_id="task_1",
            session_id="sess_1",
            tool_call_id="tc_allow_with_lease",
        )
    finally:
        reset_current_sandbox_lease(lease_token)
        reset_current_principal(principal_token)

    records = _read_decisions()
    assert result == "OK"
    assert observed == [{"command": "pwd"}]
    assert records[-1]["allowed"] is True
    assert records[-1]["sandbox_id"] == "sbx_1"
