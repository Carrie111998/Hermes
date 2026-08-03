from types import SimpleNamespace

from run_agent import AIAgent


def test_activity_summary_exposes_guard_progress_contract():
    agent = SimpleNamespace(
        _last_activity_ts=0.0,
        _last_activity_desc="tool result",
        _current_tool="terminal",
        _api_call_count=4,
        _progress_seq=7,
        _progress_evidence="7:tool result",
        _action_fingerprint="terminal:{...}",
        _failure_seq=0,
        _failure_streak=0,
        _context_tokens=1234,
        max_iterations=90,
        iteration_budget=SimpleNamespace(used=4, max_total=90),
    )
    summary = AIAgent.get_activity_summary(agent)
    assert summary["progress_seq"] == 7
    assert summary["progress_evidence"] == "7:tool result"
    assert summary["action_fingerprint"] == "terminal:{...}"
    assert summary["attempt_seq"] == 4
    assert summary["context_tokens"] == 1234


def test_touch_activity_does_not_call_heartbeat_or_tool_transport_progress():
    agent = SimpleNamespace(
        _last_activity_ts=0.0,
        _last_activity_desc="",
        _progress_seq=0,
        _progress_evidence="init",
        _action_fingerprint=None,
    )
    AIAgent._touch_activity(agent, "API call #1 completed")
    assert agent._progress_seq == 0
    AIAgent._touch_activity(agent, "tool completed: terminal")
    assert agent._progress_seq == 0
    AIAgent._touch_activity(agent, "file changed: config.yaml")
    assert agent._progress_seq == 1


def test_tool_result_progress_requires_changed_successful_result():
    agent = SimpleNamespace(
        _progress_seq=0,
        _progress_evidence="init",
        _action_fingerprint=None,
        _last_tool_result_marker=None,
    )
    AIAgent._mark_tool_result_progress(agent, "terminal", "same", args={"cmd": "x"})
    assert agent._progress_seq == 1
    AIAgent._mark_tool_result_progress(agent, "terminal", "same", args={"cmd": "x"})
    assert agent._progress_seq == 1
    AIAgent._mark_tool_result_progress(agent, "terminal", "changed", args={"cmd": "x"})
    assert agent._progress_seq == 2
