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


def _agent(session: _FakeStreamSession) -> SimpleNamespace:
    return SimpleNamespace(
        _antigravity_stream_session=session,
        messages=[],
        _session_db=None,
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
