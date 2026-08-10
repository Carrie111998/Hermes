from __future__ import annotations

import importlib.util
from pathlib import Path

from agent.transports.claude_cli import ClaudeCLITurnResult


def _load_probe_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "claude_cli_stage0_probe.py"
    spec = importlib.util.spec_from_file_location("claude_cli_stage0_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage0_probe_evaluation_requires_exact_sanitized_contract():
    probe = _load_probe_module()
    first = ClaudeCLITurnResult(
        final_text="not inspected",
        session_id="opaque-session",
        event_counts={"system": 1, "user": 1, "result": 1},
        generation=1,
    )
    second = ClaudeCLITurnResult(
        final_text="not inspected",
        session_id="opaque-session",
        event_counts={"user": 1, "result": 1},
        generation=1,
    )

    result = probe.evaluate_probe(
        version="2.1.221",
        first=first,
        second=second,
        first_pid=123,
        second_pid=123,
        spawned_argv=["wrapper", "claude", "-p", "--tools", ""],
        returncode=0,
        cleanup_seconds=0.1,
    )

    assert result == {
        "status": "PASS",
        "version": "2.1.221",
        "same_child_pid": True,
        "session_consistent": True,
        "process_generations": 1,
        "replay_acknowledgements": 2,
        "results": 2,
        "init_tools_empty": True,
        "tool_events": 0,
        "forbidden_flags": 0,
        "clean_exit": True,
        "bounded_exit": True,
    }
    assert "opaque-session" not in str(result)
    assert "not inspected" not in str(result)


def test_stage0_probe_evaluation_fails_for_forbidden_flag_or_unclean_exit():
    probe = _load_probe_module()
    turn = ClaudeCLITurnResult(
        final_text="",
        session_id="opaque-session",
        event_counts={"system": 1, "user": 1, "result": 1},
        generation=1,
    )

    result = probe.evaluate_probe(
        version="2.1.221",
        first=turn,
        second=turn,
        first_pid=123,
        second_pid=123,
        spawned_argv=["wrapper", "claude", "--dangerously-skip-permissions"],
        returncode=-1,
        cleanup_seconds=0.1,
    )

    assert result["status"] == "FAIL"
    assert result["forbidden_flags"] == 1
    assert result["clean_exit"] is False
