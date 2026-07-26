"""Live-agent /model swap coverage for the Claude Agent SDK runtime."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from run_agent import AIAgent


def _make_agent(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_mode: str,
    sdk_session=None,
) -> Any:
    agent: Any = AIAgent.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.api_key = "old-key"
    agent.api_mode = api_mode
    agent.client = MagicMock(name="OldClient") if base_url else None
    agent._client_kwargs = (
        {"api_key": "old-key", "base_url": base_url} if base_url else {}
    )
    agent._anthropic_client = None
    agent._anthropic_api_key = ""
    agent._anthropic_base_url = None
    agent._is_anthropic_oauth = False
    agent._claude_sdk_session = sdk_session
    agent._credential_pool = None
    agent._transport_cache = {}
    agent._config_context_length = None
    agent.context_compressor = None
    agent.reasoning_config = None
    agent._cached_system_prompt = "cached"
    agent._primary_runtime = {}
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._fallback_model = None
    agent._session_db = None
    agent.session_id = None
    agent.tool_progress_callback = None
    agent._interrupt_requested = False
    agent._persist_disabled = False
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent.session_api_calls = 0
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    return agent


def _switch(agent: AIAgent, **kwargs) -> None:
    with (
        patch("agent.credential_pool.load_pool", return_value=None),
        patch("hermes_cli.timeouts.get_provider_request_timeout", return_value=None),
    ):
        agent.switch_model(**kwargs)


def test_codex_to_claude_sdk_is_clientless_with_empty_url():
    agent = _make_agent(
        provider="openai-codex",
        model="gpt-5.3-codex",
        base_url="https://chatgpt.com/backend-api/codex/responses",
        api_mode="codex_responses",
    )
    agent._create_openai_client = MagicMock(
        side_effect=AssertionError("OpenAI client must not be built")
    )
    agent._session_db = MagicMock()
    agent.session_id = "sess-boundary-in"

    with patch("agent.anthropic_adapter.build_anthropic_client") as build_anthropic:
        _switch(
            agent,
            new_model="claude-opus-5",
            new_provider="claude-agent-sdk",
            api_key="claude-subscription-oauth",
            base_url="",
            api_mode="claude_agent_sdk",
        )

    agent._create_openai_client.assert_not_called()
    build_anthropic.assert_not_called()
    assert agent.provider == "claude-agent-sdk"
    assert agent.model == "claude-opus-5"
    assert agent.base_url == ""
    assert agent.api_mode == "claude_agent_sdk"
    assert agent.api_key == "claude-subscription-oauth"
    assert agent.client is None
    assert agent._client_kwargs == {}
    agent._session_db.update_claude_sdk_session_id.assert_called_once_with(
        "sess-boundary-in", None
    )


def test_switching_away_from_claude_sdk_retires_live_session():
    sdk_session = MagicMock(name="ClaudeSdkSession")
    agent = _make_agent(
        provider="claude-agent-sdk",
        model="claude-opus-5",
        base_url="",
        api_mode="claude_agent_sdk",
        sdk_session=sdk_session,
    )
    new_client = MagicMock(name="OpenRouterClient")
    agent._create_openai_client = MagicMock(return_value=new_client)
    agent._session_db = MagicMock()
    agent.session_id = "sess-boundary-out"

    _switch(
        agent,
        new_model="openai/gpt-5",
        new_provider="openrouter",
        api_key="new-key",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
    )

    sdk_session.close.assert_called_once_with()
    assert agent._claude_sdk_session is None
    assert agent.client is new_client
    agent._session_db.update_claude_sdk_session_id.assert_called_once_with(
        "sess-boundary-out", None
    )


def test_switch_away_and_back_clears_continuity_at_each_boundary():
    agent = _make_agent(
        provider="claude-agent-sdk",
        model="claude-opus-5",
        base_url="",
        api_mode="claude_agent_sdk",
        sdk_session=MagicMock(name="ClaudeSdkSession"),
    )
    agent._create_openai_client = MagicMock(return_value=MagicMock())
    agent._session_db = MagicMock()
    agent.session_id = "sess-round-trip"

    _switch(
        agent,
        new_model="openai/gpt-5",
        new_provider="openrouter",
        api_key="new-key",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
    )
    _switch(
        agent,
        new_model="claude-opus-5",
        new_provider="claude-agent-sdk",
        api_key="claude-subscription-oauth",
        base_url="",
        api_mode="claude_agent_sdk",
    )

    assert agent._session_db.update_claude_sdk_session_id.call_args_list == [
        call("sess-round-trip", None),
        call("sess-round-trip", None),
    ]


def test_claude_sdk_model_change_retires_session_for_lazy_resume(monkeypatch):
    from agent.claude_sdk_runtime import run_claude_agent_sdk_turn

    sdk_session = MagicMock(name="OldClaudeSdkSession")
    agent = _make_agent(
        provider="claude-agent-sdk",
        model="claude-sonnet-5",
        base_url="",
        api_mode="claude_agent_sdk",
        sdk_session=sdk_session,
    )
    agent._create_openai_client = MagicMock(
        side_effect=AssertionError("OpenAI client must not be built")
    )
    agent._session_db = MagicMock()
    agent.session_id = "sess-same-sdk"

    _switch(
        agent,
        new_model="claude-opus-5",
        new_provider="claude-agent-sdk",
        api_key="claude-subscription-oauth",
        base_url="",
        api_mode="claude_agent_sdk",
    )

    sdk_session.close.assert_called_once_with()
    assert agent._claude_sdk_session is None
    assert agent.model == "claude-opus-5"
    assert agent.api_mode == "claude_agent_sdk"
    agent._session_db.update_claude_sdk_session_id.assert_not_called()

    captured = {}

    class SpySession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, user_input):
            return SimpleNamespace(
                interrupted=False,
                error=None,
                thread_id="new-sdk-session",
                turn_id="new-turn",
                projected_messages=[
                    {"role": "assistant", "content": "new model response"}
                ],
                tool_iterations=0,
                final_text="new model response",
                should_retire=False,
                token_usage_last={"input_tokens": 1, "output_tokens": 1},
                token_usage_total=None,
            )

    monkeypatch.setattr(
        "agent.transports.claude_agent_sdk_session.ClaudeAgentSdkSession",
        SpySession,
    )

    result = run_claude_agent_sdk_turn(
        agent,
        user_message="continue",
        original_user_message="continue",
        messages=[{"role": "user", "content": "continue"}],
        effective_task_id="task-1",
    )

    assert captured["model"] == "claude-opus-5"
    assert result["final_response"] == "new model response"


def test_failed_unrelated_http_switch_keeps_original_runtime():
    agent = _make_agent(
        provider="openrouter",
        model="x-ai/grok-4",
        base_url="https://openrouter.ai/api/v1",
        api_mode="chat_completions",
    )
    old_client = agent.client
    old_kwargs = dict(agent._client_kwargs)
    agent._create_openai_client = MagicMock(
        side_effect=RuntimeError("simulated HTTP client build failure")
    )

    with pytest.raises(RuntimeError, match="simulated HTTP client build failure"):
        _switch(
            agent,
            new_model="MiniMax-M3",
            new_provider="custom:minimax",
            api_key="new-key",
            base_url="https://api.minimax.io/v1",
            api_mode="chat_completions",
        )

    assert agent.provider == "openrouter"
    assert agent.model == "x-ai/grok-4"
    assert agent.base_url == "https://openrouter.ai/api/v1"
    assert agent.api_mode == "chat_completions"
    assert agent.api_key == "old-key"
    assert agent.client is old_client
    assert agent._client_kwargs == old_kwargs
