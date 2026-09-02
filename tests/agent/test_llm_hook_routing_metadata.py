"""Regression coverage for routing metadata on LLM lifecycle hooks."""

from agent.turn_finalizer import finalize_turn
from tests.agent.test_turn_context import _FakeAgent as TurnContextAgent, _build
from tests.agent.test_turn_finalizer_final_response_persistence import (
    FakeAgent as TurnFinalizerAgent,
)


def test_llm_hooks_receive_gateway_routing_metadata(monkeypatch):
    events = {}

    def capture(hook_name, **payload):
        events[hook_name] = payload
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)

    pre_agent = TurnContextAgent()
    pre_agent.platform = "feishu"
    pre_agent._user_id = "user-123"
    pre_agent._chat_id = "chat-456"
    _build(pre_agent)

    post_agent = TurnFinalizerAgent()
    post_agent.platform = "feishu"
    post_agent._user_id = "user-123"
    post_agent._chat_id = "chat-456"
    finalize_turn(
        post_agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=[
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Done."},
        ],
        conversation_history=[],
        effective_task_id="task-1",
        turn_id="turn-1",
        user_message="hello",
        original_user_message="hello",
        _should_review_memory=False,
        _turn_exit_reason="text_response(final)",
    )

    expected = {
        "platform": "feishu",
        "sender_id": "user-123",
        "chat_id": "chat-456",
    }
    assert {key: events["pre_llm_call"][key] for key in expected} == expected
    assert {key: events["post_llm_call"][key] for key in expected} == expected
