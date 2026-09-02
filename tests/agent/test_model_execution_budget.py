"""Behavior contracts for the config-driven execution budget gate."""

from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent.model_execution_budget as budget


@pytest.fixture(autouse=True)
def isolated_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _agent(
    model="provider/model",
    *,
    turn="turn-1",
    task="task-1",
    session="session-1",
):
    return SimpleNamespace(
        model=model,
        provider="test-provider",
        session_id=session,
        _current_turn_id=turn,
        _current_task_id=task,
        _current_api_request_id="request-1",
    )


def _write_config(home, model_budgets):
    (home / "config.yaml").write_text(
        json.dumps({"agent": {"model_execution_budgets": model_budgets}}),
        encoding="utf-8",
    )


def test_default_config_declares_an_empty_budget_map():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"]["model_execution_budgets"] == {}


def test_unset_budget_has_zero_default_footprint(isolated_hermes_home):
    agent = _agent()

    decisions = [
        budget.decide_tool_call(agent, function_name="write_file")
        for _ in range(20)
    ]

    assert all(decision["action"] == "allow" for decision in decisions)
    assert all(decision["reason"] == "disabled" for decision in decisions)
    assert budget.execution_call_count(agent) == 0
    assert not (isolated_hermes_home / "config.yaml").exists()


def test_matching_model_budget_blocks_after_limit(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 2})
    agent = _agent()

    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "allow"
    assert budget.decide_tool_call(agent, function_name="write_file")["action"] == "allow"
    blocked = budget.decide_tool_call(agent, function_name="patch")

    assert blocked == {
        "action": "block",
        "reason": "execution_budget_exceeded",
        "count": 2,
        "limit": 2,
    }
    assert budget.execution_call_count(agent) == 2


def test_patterns_match_case_insensitively_and_provider_free_names(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"*worker*": 1})
    agent = _agent(model="Provider/Worker-Model")

    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "allow"
    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "block"


def test_most_specific_matching_pattern_wins(isolated_hermes_home):
    _write_config(
        isolated_hermes_home,
        {"*": 1, "provider/model": 3},
    )
    agent = _agent()

    decisions = [
        budget.decide_tool_call(agent, function_name="terminal")
        for _ in range(4)
    ]

    assert [decision["action"] for decision in decisions] == [
        "allow",
        "allow",
        "allow",
        "block",
    ]
    assert decisions[-1]["limit"] == 3


def test_unmatched_model_is_disabled(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"other/*": 1})
    agent = _agent()

    decisions = [
        budget.decide_tool_call(agent, function_name="terminal")
        for _ in range(10)
    ]

    assert all(decision["action"] == "allow" for decision in decisions)
    assert all(decision["reason"] == "disabled" for decision in decisions)
    assert budget.execution_call_count(agent) == 0


def test_zero_is_a_valid_budget_and_delegation_remains_exempt(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 0})
    agent = _agent()

    blocked = budget.decide_tool_call(agent, function_name="terminal")
    delegated = budget.decide_tool_call(agent, function_name="delegate_task")
    delegated_alias = budget.decide_tool_call(agent, function_name="delegate")

    assert blocked["action"] == "block"
    assert delegated["action"] == "allow"
    assert delegated["reason"] == "delegation"
    assert delegated_alias["action"] == "allow"
    assert budget.execution_call_count(agent) == 0


def test_delegation_does_not_reset_or_consume_the_same_turn(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 1})
    agent = _agent()

    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "allow"
    assert budget.decide_tool_call(agent, function_name="delegate_task")["action"] == "allow"
    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "block"


def test_new_turn_gets_a_fresh_budget_window(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 2})
    agent = _agent(turn="turn-a")

    for _ in range(2):
        assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "allow"
    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "block"

    agent._current_turn_id = "turn-b"
    assert budget.decide_tool_call(agent, function_name="terminal")["count"] == 1
    assert budget.decide_tool_call(agent, function_name="terminal")["count"] == 2
    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "block"


def test_budget_windows_are_isolated_between_agents(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 1})
    first = _agent()
    second = _agent(session="session-2")

    assert budget.decide_tool_call(first, function_name="terminal")["action"] == "allow"
    assert budget.decide_tool_call(first, function_name="terminal")["action"] == "block"
    assert budget.decide_tool_call(second, function_name="terminal")["action"] == "allow"
    assert budget.execution_call_count(second) == 1


@pytest.mark.parametrize(
    "model_budgets",
    [
        {"provider/model": -1},
        {"provider/model": True},
        {"provider/model": {"limit": 1}},
        ["not-a-map"],
        "not-a-map",
    ],
)
def test_invalid_budget_entries_fail_open(isolated_hermes_home, model_budgets):
    _write_config(isolated_hermes_home, model_budgets)
    agent = _agent()

    decisions = [
        budget.decide_tool_call(agent, function_name="terminal")
        for _ in range(8)
    ]

    assert all(decision["action"] == "allow" for decision in decisions)
    assert all(decision["reason"] == "disabled" for decision in decisions)


def test_malformed_config_fails_open(isolated_hermes_home):
    (isolated_hermes_home / "config.yaml").write_text(
        "agent: [this is not a mapping",
        encoding="utf-8",
    )

    agent = _agent()
    decisions = [
        budget.decide_tool_call(agent, function_name="terminal")
        for _ in range(8)
    ]

    assert all(decision["action"] == "allow" for decision in decisions)
    assert budget.execution_call_count(agent) == 0


def test_concurrent_calls_share_one_thread_safe_budget(isolated_hermes_home):
    _write_config(isolated_hermes_home, {"provider/model": 5})
    agent = _agent()
    barrier = threading.Barrier(7)
    decisions = []
    errors = []
    results_lock = threading.Lock()

    def call_once():
        try:
            barrier.wait(timeout=5)
            decision = budget.decide_tool_call(agent, function_name="terminal")
            with results_lock:
                decisions.append(decision["action"])
        except Exception as exc:  # pragma: no cover - diagnostic assertion
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=call_once) for _ in range(6)]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert decisions.count("allow") == 5
    assert decisions.count("block") == 1
    assert budget.execution_call_count(agent) == 5


def _middleware_agent(model="provider/model"):
    guardrails = SimpleNamespace(
        before_call=lambda name, args: SimpleNamespace(allows_execution=True),
    )
    return SimpleNamespace(
        model=model,
        session_id="session-1",
        _delegate_depth=0,
        _subagent_id="",
        _current_turn_id="turn-1",
        _current_task_id="task-1",
        _current_api_request_id="request-1",
        _tool_guardrails=guardrails,
        _touch_activity=lambda description: None,
    )


def test_execution_middleware_blocks_before_terminal_dispatch(
    isolated_hermes_home, monkeypatch
):
    _write_config(isolated_hermes_home, {"provider/model": 1})
    import agent.tool_executor as tool_executor

    # Consume the one allowed call so the next call exercises the actual seam.
    agent = _middleware_agent()
    assert budget.decide_tool_call(agent, function_name="terminal")["action"] == "allow"
    execute = Mock(return_value="should-not-run")
    monkeypatch.setattr(
        tool_executor,
        "_emit_terminal_post_tool_call",
        lambda *args, **kwargs: None,
    )

    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={"command": "true"},
        effective_task_id="task-1",
        tool_call_id="call-2",
        execute=execute,
    )

    assert outcome.blocked is True
    assert execute.call_count == 0
    assert "execution budget" in outcome.result.lower()


def test_execution_middleware_preserves_allowed_dispatch(
    isolated_hermes_home, monkeypatch
):
    _write_config(isolated_hermes_home, {"provider/model": 1})
    import agent.tool_executor as tool_executor

    agent = _middleware_agent()
    execute = Mock(return_value="ok")
    monkeypatch.setattr(tool_executor, "_begin_tool_execution", lambda *a, **k: None)
    monkeypatch.setattr(
        tool_executor,
        "_emit_terminal_post_tool_call",
        lambda *args, **kwargs: None,
    )

    outcome = tool_executor._run_agent_tool_execution_middleware(
        agent,
        function_name="terminal",
        function_args={"command": "true"},
        effective_task_id="task-1",
        tool_call_id="call-1",
        execute=execute,
    )

    assert outcome.blocked is False
    assert outcome.result == "ok"
    execute.assert_called_once_with({"command": "true"})


def test_budget_decision_does_not_mutate_prompt_or_tool_arguments(
    isolated_hermes_home,
):
    _write_config(isolated_hermes_home, {"provider/model": 1})
    agent = _agent()
    prompt = [{"role": "system", "content": "stable"}]
    args = {"command": "printf secret", "nested": {"value": 1}}
    prompt_before = copy.deepcopy(prompt)
    args_before = copy.deepcopy(args)

    decision = budget.decide_tool_call(
        agent,
        function_name="terminal",
        final_args=args,
        tool_call_id="call-1",
    )

    assert decision["action"] == "allow"
    assert prompt == prompt_before
    assert args == args_before
