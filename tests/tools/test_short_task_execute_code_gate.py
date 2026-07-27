"""Fail-closed process-boundary tests for short-task execute_code."""

from __future__ import annotations

import json

import pytest

import model_tools
from tools import code_execution_tool as cet
from tools import cronjob_tools as cron_tools
from tools import terminal_tool as terminal_module
from tools.registry import invalidate_check_fn_cache


def _policy_snapshot(*, enabled: bool) -> str:
    return json.dumps(
        {
            "schema": 2,
            "enabled": enabled,
            "soft_iteration_limit": 36,
            "max_handoffs": 8,
            "max_iterations": 90,
            "failure_limit": 2,
            "validation_error": None,
        }
    )


def _verified_managed_lane(monkeypatch, *, lane: str = "implementation") -> None:
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_LANE", lane)
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1" if lane == "review" else "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)


@pytest.mark.parametrize(
    "snapshot",
    [
        _policy_snapshot(enabled=True),
        "{not-valid-json",
    ],
)
def test_enabled_or_invalid_worker_policy_hides_and_blocks_execute_code(
    monkeypatch, snapshot
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setattr(cet, "check_sandbox_requirements", lambda: True)

    def forbidden_backend_probe():
        pytest.fail("execute_code reached a sandbox or RPC side effect")

    monkeypatch.setattr(
        "tools.terminal_tool._get_env_config", forbidden_backend_probe
    )
    assert cet._check_execute_code_requirements() is False
    result = json.loads(cet.execute_code("print('must not run')"))
    assert result["status"] == "blocked"
    assert result["tool_calls_made"] == 0
    assert result["duration_seconds"] == 0


@pytest.mark.parametrize(
    "worker,snapshot",
    [
        (False, None),
        (True, _policy_snapshot(enabled=False)),
    ],
)
def test_no_worker_or_disabled_policy_preserves_execute_code_baseline(
    monkeypatch, worker, snapshot
):
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if snapshot is None:
        monkeypatch.delenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
        )
    else:
        monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setattr(cet, "check_sandbox_requirements", lambda: True)
    assert cet._check_execute_code_requirements() is True

    class BaselineReached(RuntimeError):
        pass

    def baseline_backend_probe():
        raise BaselineReached("normal execute_code path reached")

    monkeypatch.setattr(
        "tools.terminal_tool._get_env_config", baseline_backend_probe
    )
    with pytest.raises(BaselineReached, match="normal execute_code path reached"):
        cet.execute_code("print('baseline')")


@pytest.mark.parametrize(
    "snapshot",
    [
        _policy_snapshot(enabled=True),
        "{not-valid-json",
    ],
)
def test_managed_worker_schema_is_a_final_allowlist(monkeypatch, snapshot):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    _verified_managed_lane(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli"], quiet_mode=True
    )
    names = {tool["function"]["name"] for tool in definitions}

    assert {
        "read_file",
        "search_files",
        "write_file",
        "patch",
        "kanban_show",
        "kanban_complete",
    }.issubset(names)
    assert names <= model_tools._SHORT_TASK_WORKER_ALLOWED_TOOLS
    assert {
        "terminal",
        "process",
        "execute_code",
        "cronjob",
        "delegate_task",
        "tool_search",
        "tool_describe",
        "tool_call",
    }.isdisjoint(names)


def test_managed_review_schema_is_read_only_file_and_lifecycle(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    _verified_managed_lane(monkeypatch, lane="review")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=False),
    )
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    model_tools._clear_tool_defs_cache()
    invalidate_check_fn_cache()

    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=["hermes-cli"], quiet_mode=True
    )
    names = {tool["function"]["name"] for tool in definitions}

    assert {
        "read_file",
        "search_files",
        "kanban_show",
        "kanban_complete",
    }.issubset(names)
    assert names <= model_tools._SHORT_TASK_REVIEW_WORKER_ALLOWED_TOOLS
    assert {
        "terminal",
        "process",
        "execute_code",
        "write_file",
        "patch",
        "cronjob",
        "delegate_task",
        "tool_search",
        "tool_describe",
        "tool_call",
    }.isdisjoint(names)


@pytest.mark.parametrize("tool_name", ["write_file", "patch"])
def test_managed_review_direct_write_dispatch_is_rejected(monkeypatch, tool_name):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    _verified_managed_lane(monkeypatch, lane="review")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=False),
    )

    def forbidden_dispatch(*_args, **_kwargs):
        pytest.fail("managed review write reached registry dispatch")

    monkeypatch.setattr(model_tools.registry, "dispatch", forbidden_dispatch)
    result = json.loads(model_tools.handle_function_call(tool_name, {}))

    assert result["success"] is False
    assert "unavailable in this automatic short-task worker" in result["error"]


def test_managed_review_direct_terminal_calls_fail_before_execution(
    monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    _verified_managed_lane(monkeypatch, lane="review")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=False),
    )

    def forbidden_dispatch(*_args, **_kwargs):
        pytest.fail("managed review reached registry dispatch")

    def forbidden_backend_probe():
        pytest.fail("managed review reached terminal backend configuration")

    monkeypatch.setattr(model_tools.registry, "dispatch", forbidden_dispatch)
    via_orchestrator = json.loads(
        model_tools.handle_function_call("terminal", {"command": "pwd"})
    )
    assert via_orchestrator["success"] is False
    assert "unavailable in this automatic short-task worker" in (
        via_orchestrator["error"]
    )

    monkeypatch.setattr(
        terminal_module, "_get_env_config", forbidden_backend_probe
    )
    via_runtime = json.loads(terminal_module.terminal_tool(command="pwd"))
    assert via_runtime["status"] == "blocked"
    assert "OS-isolated verifier" in via_runtime["error"]


@pytest.mark.parametrize(
    "snapshot",
    [_policy_snapshot(enabled=True), "{not-valid-json"],
)
def test_final_allowlist_removes_unknown_alias_and_dynamic_schema(
    monkeypatch, snapshot
):
    definitions = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "cronjob_alias"}},
        {"type": "function", "function": {"name": "memory_plugin"}},
        {"type": "function", "function": {"name": "tool_call"}},
    ]
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    _verified_managed_lane(monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)

    filtered = model_tools.restrict_short_task_worker_tool_definitions(
        definitions
    )

    assert [tool["function"]["name"] for tool in filtered] == ["read_file"]

    for key in (
        "HERMES_KANBAN_MANAGED_LANE",
        "HERMES_KANBAN_REVIEW_MODE",
        "HERMES_KANBAN_MANAGED_BOOTSTRAP",
        "HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=False),
    )
    assert model_tools.restrict_short_task_worker_tool_definitions(
        definitions
    ) == definitions


@pytest.mark.parametrize(
    "tool_name",
    [
        "cronjob_alias",
        "process",
        "delegate_task",
        "memory_plugin",
        "tool_search",
        "tool_describe",
        "tool_call",
    ],
)
def test_direct_dispatch_cannot_bypass_hidden_or_dynamic_tool_gate(
    monkeypatch, tool_name
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    _verified_managed_lane(monkeypatch)
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=True),
    )

    def forbidden_dispatch(*_args, **_kwargs):
        pytest.fail("hidden tool reached registry dispatch")

    monkeypatch.setattr(model_tools.registry, "dispatch", forbidden_dispatch)

    result = json.loads(model_tools.handle_function_call(tool_name, {}))

    assert result["success"] is False
    assert "unavailable in this automatic short-task worker" in result["error"]


def test_disabled_worker_preserves_direct_dispatch_baseline(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        _policy_snapshot(enabled=False),
    )
    reached = []
    monkeypatch.setattr(
        model_tools.registry,
        "dispatch",
        lambda name, args, **_kwargs: reached.append((name, args)) or "ok",
    )

    result = model_tools.handle_function_call("cronjob_alias", {"task": "x"})

    assert result == "ok"
    assert reached == [("cronjob_alias", {"task": "x"})]


@pytest.mark.parametrize(
    "snapshot",
    [
        _policy_snapshot(enabled=True),
        "{not-valid-json",
    ],
)
def test_managed_worker_cron_gate_blocks_schema_and_handler(
    monkeypatch, snapshot
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_short")
    monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")

    def forbidden_create(*_args, **_kwargs):
        pytest.fail("cron creation side effect was reached")

    monkeypatch.setattr(cron_tools, "create_job", forbidden_create)

    assert cron_tools.check_cronjob_requirements() is False
    result = json.loads(
        cron_tools.cronjob(
            action="create",
            schedule="every 1m",
            prompt="run later",
            workdir="/tmp",
        )
    )
    assert result["success"] is False
    assert "disabled for automatic short-task workers" in result["error"]


@pytest.mark.parametrize(
    "worker,snapshot",
    [
        (False, None),
        (True, _policy_snapshot(enabled=False)),
    ],
)
def test_ordinary_or_disabled_worker_preserves_cron_baseline(
    monkeypatch, worker, snapshot
):
    if worker:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_review")
    else:
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    if snapshot is None:
        monkeypatch.delenv(
            "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", raising=False
        )
    else:
        monkeypatch.setenv("HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY", snapshot)
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    calls = []
    monkeypatch.setattr(
        cron_tools,
        "list_jobs",
        lambda *, include_disabled: calls.append(include_disabled) or [],
    )

    assert cron_tools.check_cronjob_requirements() is True
    result = json.loads(cron_tools.cronjob(action="list"))
    assert calls == [False]
    assert "error" not in result
