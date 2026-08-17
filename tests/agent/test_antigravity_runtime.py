"""Hermes runtime contract tests for the Antigravity structured adapter."""

from __future__ import annotations

from types import SimpleNamespace

from agent.antigravity_runtime import run_antigravity_mcp_turn
from agent.transports.antigravity_stream_json import AntigravityTurnResult


class _FakeStreamSession:
    def __init__(self, result: AntigravityTurnResult):
        self.result = result
        self.calls = []

    def run_turn(self, user_input, *, system_prompt=None):
        self.calls.append((user_input, system_prompt))
        return self.result

    def close(self):
        pass


class _FakeSessionDb:
    def __init__(self):
        self.calls = []

    def update_token_counts(self, session_id, **kwargs):
        self.calls.append((session_id, kwargs))


class _FakeContextCompressor:
    def __init__(self):
        self.updates = []

    def update_from_response(self, usage):
        self.updates.append(usage)


def _agent(session: _FakeStreamSession) -> SimpleNamespace:
    return SimpleNamespace(
        _antigravity_stream_session=session,
        messages=[],
        _session_db=None,
        _session_db_created=True,
        session_id="session-1",
        base_url="mcp://antigravity-cli",
        context_compressor=_FakeContextCompressor(),
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_api_calls=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        session_cost_source="none",
        _iters_since_skill=0,
        _skill_nudge_interval=0,
        valid_tool_names=[],
        _sync_external_memory_for_turn=lambda **kwargs: None,
        _flush_messages_to_session_db=lambda messages: None,
        _spawn_background_review=lambda **kwargs: None,
        session_cwd="/tmp",
        model="antigravity/default",
        provider="antigravity",
    )


def test_antigravity_runtime_passes_hermes_system_prompt_and_fails_closed():
    fake = _FakeStreamSession(
        AntigravityTurnResult(error="Antigravity stream ended without a terminal result")
    )
    result = run_antigravity_mcp_turn(
        agent=_agent(fake),
        user_message="checkpoint",
        original_user_message="checkpoint",
        messages=[],
        effective_task_id="task-1",
        should_review_memory=False,
        system_prompt="Hermes skills and checkpoint governance",
    )

    assert fake.calls == [("checkpoint", "Hermes skills and checkpoint governance")]
    assert result["completed"] is False
    assert result["partial"] is True
    assert "terminal result" in result["final_response"]


def test_antigravity_runtime_records_structured_usage_in_hermes_accounting():
    fake = _FakeStreamSession(
        AntigravityTurnResult(
            final_text="done",
            completed=True,
            usage={"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
        )
    )
    agent = _agent(fake)
    session_db = _FakeSessionDb()
    agent._session_db = session_db

    result = run_antigravity_mcp_turn(
        agent=agent,
        user_message="count this",
        original_user_message="count this",
        messages=[],
        effective_task_id="task-usage",
        should_review_memory=False,
        system_prompt="Hermes system",
    )

    assert result["prompt_tokens"] == 12
    assert result["completion_tokens"] == 7
    assert result["total_tokens"] == 19
    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 12
    assert agent.session_completion_tokens == 7
    assert agent.session_total_tokens == 19
    assert agent.session_input_tokens == 12
    assert agent.session_output_tokens == 7
    assert agent.context_compressor.updates == [
        {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "input_tokens": 12,
            "output_tokens": 7,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        }
    ]
    assert session_db.calls[0][0] == "session-1"
    assert session_db.calls[0][1]["input_tokens"] == 12
    assert session_db.calls[0][1]["output_tokens"] == 7
    assert session_db.calls[0][1]["api_call_count"] == 1


def test_antigravity_runtime_counts_call_without_fabricating_missing_usage():
    fake = _FakeStreamSession(AntigravityTurnResult(final_text="done", completed=True))
    agent = _agent(fake)
    session_db = _FakeSessionDb()
    agent._session_db = session_db

    result = run_antigravity_mcp_turn(
        agent=agent,
        user_message="no usage",
        original_user_message="no usage",
        messages=[],
        effective_task_id="task-no-usage",
        should_review_memory=False,
    )

    assert result["completed"] is True
    assert result["prompt_tokens"] is None
    assert result["completion_tokens"] is None
    assert result["total_tokens"] is None
    assert agent.session_api_calls == 1
    assert agent.session_prompt_tokens == 0
    assert agent.session_completion_tokens == 0
    assert agent.session_total_tokens == 0
    assert agent.context_compressor.updates == []
    assert session_db.calls[0][1]["api_call_count"] == 1
    assert "input_tokens" not in session_db.calls[0][1]
    assert "output_tokens" not in session_db.calls[0][1]
