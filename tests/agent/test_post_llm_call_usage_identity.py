"""Behavior contract for post-turn usage and sender attribution."""

from tests.agent.test_turn_finalizer_cleanup_guard import _StubAgent, _run


def test_post_llm_call_receives_usage_and_sender_id(monkeypatch):
    calls = []

    def capture(hook_name, **kwargs):
        calls.append((hook_name, kwargs))
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)
    agent = _StubAgent(raise_in=())
    agent._user_id = "gateway-user-42"
    agent.session_input_tokens = 120
    agent.session_output_tokens = 30
    agent.session_cache_read_tokens = 80
    agent.session_cache_write_tokens = 10
    agent.session_reasoning_tokens = 4
    agent.session_prompt_tokens = 120
    agent.session_completion_tokens = 30
    agent.session_total_tokens = 150

    _run(
        agent,
        final_response="done",
        api_call_count=1,
        turn_exit_reason="text_response(final)",
    )

    payload = next(kwargs for name, kwargs in calls if name == "post_llm_call")
    assert payload["sender_id"] == "gateway-user-42"
    assert payload["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_tokens": 80,
        "cache_write_tokens": 10,
        "reasoning_tokens": 4,
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
    }


def test_post_llm_call_uses_empty_sender_id_outside_gateway(monkeypatch):
    calls = []

    def capture(hook_name, **kwargs):
        calls.append((hook_name, kwargs))
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", capture)
    agent = _StubAgent(raise_in=())

    _run(
        agent,
        final_response="done",
        api_call_count=1,
        turn_exit_reason="text_response(final)",
    )

    payload = next(kwargs for name, kwargs in calls if name == "post_llm_call")
    assert payload["sender_id"] == ""
