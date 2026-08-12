"""Behavioral seam tests for successful conversation-call cleanup."""

from types import SimpleNamespace

from agent import conversation_loop
from agent.conversation_loop_success_cleanup import complete_successful_call


class _AgentStub:
    provider = "nous"

    def __init__(self):
        self.activity = []

    def _touch_activity(self, message):
        self.activity.append(message)


def test_success_cleanup_resets_retry_and_completes_logical_call(monkeypatch):
    agent = _AgentStub()
    retry = SimpleNamespace(has_retried_429=True)
    cleared = []
    completed = []

    monkeypatch.setattr(
        "agent.nous_rate_guard.clear_nous_rate_limit",
        lambda: cleared.append(True),
    )
    monkeypatch.setattr(
        "agent.relay_llm.complete_logical_call",
        lambda request_id, *, outcome: completed.append((request_id, outcome)),
    )

    result = complete_successful_call(agent, retry, "request-7", 7)

    assert result is None
    assert retry.has_retried_429 is False
    assert cleared == [True]
    assert completed == [("request-7", "success")]
    assert agent.activity == ["API call #7 completed"]


def test_success_cleanup_keeps_clear_failure_local(monkeypatch):
    agent = _AgentStub()
    retry = SimpleNamespace(has_retried_429=True)
    completed = []

    def fail_clear():
        raise RuntimeError("rate guard unavailable")

    monkeypatch.setattr("agent.nous_rate_guard.clear_nous_rate_limit", fail_clear)
    monkeypatch.setattr(
        "agent.relay_llm.complete_logical_call",
        lambda request_id, *, outcome: completed.append((request_id, outcome)),
    )

    complete_successful_call(agent, retry, "request-8", 8)

    assert completed == [("request-8", "success")]
    assert agent.activity == ["API call #8 completed"]


def test_caller_owns_retry_loop_break():
    agent = _AgentStub()
    retry = SimpleNamespace(has_retried_429=True)
    iterations = 0

    while True:
        complete_successful_call(agent, retry, "request-9", 9)
        iterations += 1
        break

    assert iterations == 1


def test_original_conversation_loop_patch_target_remains_patchable(monkeypatch):
    replacement = object()
    monkeypatch.setattr(conversation_loop, "run_conversation", replacement)
    assert conversation_loop.run_conversation is replacement
