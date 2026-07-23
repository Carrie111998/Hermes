from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.job_diagnostics import (
    TimingCategory,
    classify_tool_timing,
    start_agent_job_tracker,
    timing_breakdown,
)


def _agent(**overrides):
    values = {
        "session_id": "session-1",
        "platform": "telegram",
        "provider": "openai-codex",
        "model": "gpt-test",
        "_memory_write_origin": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_agent_tracker_persists_runtime_identity_and_timings(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    tracker = start_agent_job_tracker(
        _agent(),
        effective_task_id="task-1",
        turn_id="turn-1",
    )

    assert tracker is not None
    tracker.record_span(
        TimingCategory.MODEL_WAIT,
        100,
        105,
        label="provider call",
    )
    tracker.record_tool(
        "terminal",
        {"command": "scripts/run_tests.sh tests/unit.py"},
        105,
        112,
    )
    tracker.finish(
        failed=False,
        interrupted=False,
        summary="agent turn completed",
        exit_reason="completed",
    )

    state = tracker.store.load("session:session-1")
    lane = state["lanes"]["task:task-1"]
    metrics = timing_breakdown(state, now=112, lane_id="task:task-1")
    assert lane["session_id"] == "session-1"
    assert lane["task_id"] == "task-1"
    assert lane["platform"] == "telegram"
    assert lane["provider"] == "openai-codex"
    assert lane["model"] == "gpt-test"
    assert lane["status"] == "completed"
    assert metrics["model_wait"] == 5
    assert metrics["test"] == 7


def test_background_review_turn_records_review_phase(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    tracker = start_agent_job_tracker(
        _agent(_memory_write_origin="background_review"),
        effective_task_id="review-task",
        turn_id="review-turn",
    )
    assert tracker is not None
    tracker.finish(
        failed=False,
        interrupted=False,
        summary="review complete",
        exit_reason="completed",
    )

    state = tracker.store.load("session:session-1")
    lane = state["lanes"]["task:review-task"]
    assert lane["read_only"] is True
    assert lane["phases"][0]["category"] == "review"
    assert timing_breakdown(state)["review"] >= 0


def test_tool_timing_classifier_separates_test_review_and_evidence():
    assert (
        classify_tool_timing(
            "terminal",
            {"command": "scripts/run_tests.sh tests/foo.py"},
        )
        == "test"
    )
    assert (
        classify_tool_timing(
            "terminal",
            {"command": "git diff --check"},
        )
        == "review"
    )
    assert (
        classify_tool_timing(
            "terminal",
            {"command": "shasum -a 256 artifact.tar"},
        )
        == "evidence_generation"
    )
    assert classify_tool_timing("read_file", {"path": "x"}) == "tool_execution"
    assert classify_tool_timing("clarify", {}) == "blocked_idle"
