from types import SimpleNamespace

from agent import background_review
from run_agent import AIAgent
from tools.process_registry import process_registry
from tools.todo_tool import TodoStore


def _agent(**overrides):
    values = {
        "_review_task_terminal_only": True,
        "_review_idempotent": True,
        "_review_start_delay_seconds": 0.0,
        "_review_max_wait_seconds": 0.0,
        "_foreground_turn_active": False,
        "_todo_store": TodoStore(),
        "session_id": "session-test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_terminal_gate_rejects_active_todo(monkeypatch):
    monkeypatch.setattr(process_registry, "has_active_processes", lambda _task_id: False)
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [])
    agent = _agent()
    agent._todo_store.write([
        {"id": "work", "content": "still running", "status": "in_progress"}
    ])

    assert not background_review.task_is_terminal_for_review(
        agent,
        final_response="intermediate",
        completed=True,
        interrupted=False,
        failed=False,
        task_id="task-test",
    )


def test_terminal_gate_accepts_completed_task(monkeypatch):
    monkeypatch.setattr(process_registry, "has_active_processes", lambda _task_id: False)
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [])
    agent = _agent()
    agent._todo_store.write([
        {"id": "work", "content": "done", "status": "completed"}
    ])

    assert background_review.task_is_terminal_for_review(
        agent,
        final_response="done",
        completed=True,
        interrupted=False,
        failed=False,
        task_id="task-test",
    )


def test_terminal_gate_rejects_active_process_and_delegation(monkeypatch):
    agent = _agent()
    monkeypatch.setattr(process_registry, "has_active_processes", lambda _task_id: True)
    monkeypatch.setattr("tools.async_delegation.list_async_delegations", lambda: [])
    assert not background_review.task_is_terminal_for_review(
        agent,
        final_response="done",
        completed=True,
        interrupted=False,
        failed=False,
        task_id="task-test",
    )

    monkeypatch.setattr(process_registry, "has_active_processes", lambda _task_id: False)
    monkeypatch.setattr(
        "tools.async_delegation.list_async_delegations",
        lambda: [{"status": "running", "parent_session_id": agent.session_id}],
    )
    assert not background_review.task_is_terminal_for_review(
        agent,
        final_response="done",
        completed=True,
        interrupted=False,
        failed=False,
        task_id="task-test",
    )


def test_review_completion_is_claimed_once(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    agent = _agent()

    first = background_review.claim_review_completion(agent, "session-test:turn-1")
    second = background_review.claim_review_completion(agent, "session-test:turn-1")

    assert first is not None
    assert first.exists()
    assert second is None


def test_spawn_starts_once_for_duplicate_completion(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(
        background_review,
        "_run_review_in_thread",
        lambda _agent, _messages, _prompt: calls.append("review"),
    )

    class ImmediateThread:
        def __init__(self, *, target, daemon, name):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("run_agent.threading.Thread", ImmediateThread)
    agent = AIAgent.__new__(AIAgent)
    setattr(agent, "_review_task_terminal_only", True)
    setattr(agent, "_review_idempotent", True)
    setattr(agent, "_review_start_delay_seconds", 0.0)
    setattr(agent, "_review_max_wait_seconds", 0.0)
    setattr(agent, "_foreground_turn_active", False)
    setattr(agent, "session_id", "session-test")

    assert agent._spawn_background_review(
        messages_snapshot=[],
        review_skills=True,
        completion_id="session-test:turn-1",
    )
    assert not agent._spawn_background_review(
        messages_snapshot=[],
        review_skills=True,
        completion_id="session-test:turn-1",
    )
    assert calls == ["review"]


def test_bounded_review_wait_refuses_active_turn():
    agent = _agent(_foreground_turn_active=True)
    assert not background_review.wait_until_review_idle(agent)
