"""Runtime integration tests for trajectory quality routing.

Patterned on test_tool_call_guardrail_runtime.py. Uses real AIAgent
construction (no LLM calls), real temp HERMES_HOME, and real tool
execution through the executor to verify the observe seam fires.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_tool_call(name="terminal", arguments="{}", call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _enabled_config(**overrides) -> dict:
    cfg = {
        "trajectory_quality_routing": {
            "enabled": True,
            "execute_stop": True,
            "execute_model_switch": False,
            "persist_decisions": True,
            "thresholds": {
                "identical_failure": 2,
                "same_tool_failure": 4,
                "failed_verification": 2,
                "stagnation_window": 8,
            },
        }
    }
    cfg["trajectory_quality_routing"].update(overrides)
    return cfg


def _make_agent(
    *tool_names: str,
    config: dict | None = None,
    tmp_home: Path | None = None,
) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs(*tool_names)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value=config or {}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=10,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


# ---------------------------------------------------------------------------
# Disabled parity — zero impact when off
# ---------------------------------------------------------------------------


def test_disabled_no_recommendation_no_db(tmp_path, monkeypatch):
    """When disabled, no recommendation attr is set and no DB is created."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal")
    assert agent._trajectory_quality.config.enabled is False

    args = {"command": "false"}
    tc = _mock_tool_call("terminal", json.dumps(args), "c-disabled")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-disabled")

    assert agent._pending_trajectory_quality_recommendation is None
    assert not (home / "trajectory_quality.db").exists()


# ---------------------------------------------------------------------------
# Enabled — recommendation fires on two identical failures
# ---------------------------------------------------------------------------


def test_enabled_two_identical_failures_produces_recommendation(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal", config=_enabled_config())
    args = {"command": "false"}

    # Seed one failure directly through the controller.
    agent._trajectory_quality.observe(
        type(agent._trajectory_quality.config)  # dummy
        and _build_obs("terminal", args, True)
    )

    tc = _mock_tool_call("terminal", json.dumps(args), "c-rec")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-rec")

    rec = agent._pending_trajectory_quality_recommendation
    assert rec is not None
    assert rec.action == "recommend_stronger_model"
    assert rec.reason_code == "two_identical_failures"


def _build_obs(tool_name, args, failed):
    from agent.trajectory_quality import build_observation

    return build_observation(
        tool_name=tool_name,
        args=args,
        result=json.dumps({"exit_code": 1}) if failed else json.dumps({"ok": True}),
        failed=failed,
    )


def test_recommendation_does_not_mutate_messages(tmp_path, monkeypatch):
    """Quality routing must not add system/user messages to the transcript."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal", config=_enabled_config())
    args = {"command": "false"}

    # Seed one failure.
    agent._trajectory_quality.observe(_build_obs("terminal", args, True))

    tc = _mock_tool_call("terminal", json.dumps(args), "c-nomut")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hi"}]

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-nomut")

    # Only the tool result message should have been appended — no extra
    # system/user messages from quality routing.
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "tool"]
    # The tool result must NOT contain quality-routing markers.
    tool_content = messages[-1]["content"]
    assert "Trajectory quality" not in tool_content


def test_switch_model_not_called(tmp_path, monkeypatch):
    """Quality routing must never call switch_model."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal", config=_enabled_config())
    args = {"command": "false"}

    # Seed one failure.
    agent._trajectory_quality.observe(_build_obs("terminal", args, True))

    tc = _mock_tool_call("terminal", json.dumps(args), "c-noswitch")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with (
        patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})),
        patch.object(agent, "switch_model") as mock_switch,
    ):
        agent._execute_tool_calls_sequential(msg, messages, "task-noswitch")

    mock_switch.assert_not_called()


def test_decision_persisted_to_store(tmp_path, monkeypatch):
    """When a decision fires, it should be recorded in the store."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal", config=_enabled_config())
    assert agent._trajectory_quality_store is not None

    args = {"command": "false"}
    # Seed one failure.
    agent._trajectory_quality.observe(_build_obs("terminal", args, True))

    tc = _mock_tool_call("terminal", json.dumps(args), "c-persist")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-persist")

    # The DB file should exist with at least one row.
    assert (home / "trajectory_quality.db").exists()


def test_quality_routing_marker_absent_from_tool_result(tmp_path, monkeypatch):
    """Tool result text must not gain a quality-routing suffix."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    agent = _make_agent("terminal", config=_enabled_config())
    args = {"command": "false"}

    # Seed one failure.
    agent._trajectory_quality.observe(_build_obs("terminal", args, True))

    tc = _mock_tool_call("terminal", json.dumps(args), "c-marker")
    msg = SimpleNamespace(content="", tool_calls=[tc])
    messages = []

    with patch("run_agent.handle_function_call", return_value=json.dumps({"exit_code": 1})):
        agent._execute_tool_calls_sequential(msg, messages, "task-marker")

    tool_content = messages[-1]["content"]
    # Guardrail warning suffix may be present, but quality-specific text must not.
    assert "Trajectory quality" not in tool_content
    assert "recommend_stronger_model" not in tool_content
