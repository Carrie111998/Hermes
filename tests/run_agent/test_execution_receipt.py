"""Integration coverage for authoritative per-turn execution receipts."""

import pytest

from run_agent import AIAgent
from tools.process_registry import process_registry


def _agent_without_init():
    agent = AIAgent.__new__(AIAgent)
    agent._current_task_id = None
    return agent


def _result(*, provider="openai", evidence=None, response="I’ll keep working."):
    return {
        "provider": provider,
        "final_response": response,
        "messages": [{"role": "assistant", "content": response}],
        "interrupted": False,
        "turn_execution_evidence": evidence or {"tool_calls": []},
    }


@pytest.mark.parametrize("provider", ["openai", "anthropic", "custom"])
def test_actual_agent_attaches_provider_neutral_structured_status(monkeypatch, provider):
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *args, **kwargs: _result(provider=provider),
    )

    result = _agent_without_init().run_conversation("inspect", task_id=None)

    assert result["execution_receipt"] == {
        "status": "not_started",
        "tool_calls": 0,
        "active_processes": [],
        "exited_processes": [],
    }
    assert result["execution_status"]["status"] == "not_started"
    assert result["execution_status"]["text"] == (
        "Execution status: not started; no tools ran and no managed process was started this turn."
    )
    assert result["final_response"] == "I’ll keep working."


def test_actual_agent_uses_precompression_turn_evidence(monkeypatch):
    # The returned transcript intentionally contains no tool messages, as it
    # would after compression. Stable evidence captured during execution wins.
    evidence = {"tool_calls": [{"name": "read_file", "call_id": "call-1"}]}
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *args, **kwargs: _result(evidence=evidence, response="Done."),
    )

    result = _agent_without_init().run_conversation(
        "inspect", conversation_history=[{"role": "user", "content": "old"}]
    )

    assert result["execution_receipt"]["tool_calls"] == 1
    assert result["execution_receipt"]["status"] == "completed"


@pytest.mark.parametrize("final_status", ["running", "exited"])
def test_actual_agent_excludes_preexisting_process_from_turn(monkeypatch, final_status):
    before = [{"session_id": "old", "status": "running"}]
    after = [{"session_id": "old", "status": final_status, "exit_code": 0}]
    snapshots = iter([before, after])
    monkeypatch.setattr(process_registry, "list_sessions", lambda task_id=None: next(snapshots))
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *args, **kwargs: _result(response="Done."),
    )

    result = _agent_without_init().run_conversation("inspect", task_id=None)

    assert result["execution_receipt"]["active_processes"] == []
    assert result["execution_receipt"]["exited_processes"] == []


@pytest.mark.parametrize(
    ("session", "expected_status"),
    [
        ({"session_id": "new", "status": "running"}, "active"),
        ({"session_id": "new", "status": "exited", "exit_code": 0}, "exited"),
    ],
)
def test_actual_agent_attributes_new_managed_process_with_task_id_none(
    monkeypatch, session, expected_status
):
    snapshots = iter([[], [session]])
    monkeypatch.setattr(process_registry, "list_sessions", lambda task_id=None: next(snapshots))
    monkeypatch.setattr(
        "agent.conversation_loop.run_conversation",
        lambda *args, **kwargs: _result(response="Done."),
    )

    result = _agent_without_init().run_conversation("inspect", task_id=None)

    assert result["execution_receipt"]["status"] == expected_status
    if expected_status == "active":
        assert result["execution_receipt"]["active_processes"] == ["new"]
    else:
        assert result["execution_receipt"]["exited_processes"] == [
            {"session_id": "new", "exit_code": 0}
        ]
