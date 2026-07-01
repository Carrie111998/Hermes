"""A1.8 execute_code nested write-sink guard tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import invoke_tool


def _make_agent(classification: str | None = "C2") -> SimpleNamespace:
    agent = SimpleNamespace()
    if classification is not None:
        agent.hl_aos_taint_classification = classification
    agent.hl_aos_classification_source = "hl_aos_frozen"
    agent.session_id = "test-session"
    agent._current_turn_id = ""
    agent._current_api_request_id = ""
    agent._memory_manager = None
    agent.valid_tool_names = {"execute_code", "write_file", "patch", "read_file", "terminal"}
    return agent


def _invoke_execute_code(agent: SimpleNamespace, code: str) -> dict:
    raw = invoke_tool(
        agent,
        "execute_code",
        {"code": code},
        "task-a1-8",
        pre_tool_block_checked=True,
        skip_tool_request_middleware=True,
    )
    return json.loads(raw)


def test_c2_execute_code_raw_file_write_denied_before_script_spawn(tmp_path):
    """C2 execute_code cannot bypass write guards with raw Python file APIs."""
    target = tmp_path / "raw_nested_write.txt"
    code = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('C2 leaked through raw Python', encoding='utf-8')\n"
        "print('script-ran')\n"
    )

    result = _invoke_execute_code(_make_agent("C2"), code)

    assert result["status"] == "blocked"
    assert result["denied_by"] == "a1_8_execute_code_write_guard"
    assert "execute_code" in result["error"]
    assert not target.exists()


def test_c2_execute_code_nested_write_file_tool_denied_before_rpc_dispatch(tmp_path):
    """C2 execute_code cannot bypass write guards through hermes_tools.write_file."""
    target = tmp_path / "rpc_nested_write.txt"
    code = (
        "from hermes_tools import write_file\n"
        f"write_file({str(target)!r}, 'C2 leaked through sandbox RPC')\n"
        "print('script-ran')\n"
    )

    result = _invoke_execute_code(_make_agent("C2"), code)

    assert result["status"] == "blocked"
    assert result["denied_by"] == "a1_8_execute_code_write_guard"
    assert not target.exists()


def test_c0_execute_code_without_write_intent_still_runs():
    """C0 public execute_code remains available for non-write scripts."""
    result = _invoke_execute_code(_make_agent("C0"), "print('safe-output')")

    assert result["status"] == "success"
    assert "safe-output" in result["output"]
