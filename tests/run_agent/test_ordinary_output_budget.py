"""Ordinary-generation output budgets stay bounded and provider-safe."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.transports.chat_completions import ChatCompletionsTransport
from providers import get_provider_profile
from run_agent import AIAgent


def _messages():
    return [{"role": "user", "content": "hello"}]


def _custom_agent(tmp_path, monkeypatch, *, max_tokens, platform=""):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        return AIAgent(
            session_id="bounded-output-test",
            api_key="no-key-required",
            base_url="http://127.0.0.1:11435/v1",
            provider="custom",
            model="qwen-local",
            max_tokens=max_tokens,
            platform=platform,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )


def test_local_custom_provider_none_uses_conservative_output_limit():
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=get_provider_profile("custom"),
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )

    assert kwargs["max_tokens"] == 4096
    assert kwargs["max_tokens"] not in {-1, 65_536, 131_072}


def test_legacy_custom_provider_none_still_sends_bounded_limit():
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=None,
        is_custom_provider=True,
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )

    assert kwargs["max_tokens"] == 4096


def test_custom_request_override_none_cannot_remove_bound():
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=get_provider_profile("custom"),
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
        request_overrides={"max_tokens": None},
    )

    assert kwargs["max_tokens"] == 4096


def test_negative_request_override_is_rejected_with_classification():
    transport = ChatCompletionsTransport()

    with pytest.raises(ValueError, match="invalid_non_positive"):
        transport.build_kwargs(
            model="qwen-local",
            messages=_messages(),
            provider_profile=get_provider_profile("custom"),
            max_tokens=None,
            max_tokens_param_fn=lambda value: {"max_tokens": value},
            request_overrides={"max_tokens": -1},
        )


@pytest.mark.parametrize("nested_value", [None, -1])
def test_custom_extra_body_cannot_override_bound_with_unbounded_value(nested_value):
    transport = ChatCompletionsTransport()

    if nested_value is None:
        kwargs = transport.build_kwargs(
            model="qwen-local",
            messages=_messages(),
            provider_profile=get_provider_profile("custom"),
            max_tokens=None,
            max_tokens_param_fn=lambda value: {"max_tokens": value},
            request_overrides={"extra_body": {"max_tokens": nested_value}},
        )
        assert kwargs["max_tokens"] == 4096
        assert "max_tokens" not in kwargs.get("extra_body", {})
    else:
        with pytest.raises(ValueError, match="invalid_non_positive"):
            transport.build_kwargs(
                model="qwen-local",
                messages=_messages(),
                provider_profile=get_provider_profile("custom"),
                max_tokens=None,
                max_tokens_param_fn=lambda value: {"max_tokens": value},
                request_overrides={"extra_body": {"max_tokens": nested_value}},
            )


@pytest.mark.parametrize(
    "nested_key", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
def test_positive_extra_body_limit_uses_provider_parameter_without_collision(
    nested_key,
):
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=get_provider_profile("custom"),
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_completion_tokens": value},
        request_overrides={"extra_body": {nested_key: 7777}},
    )

    assert kwargs["max_completion_tokens"] == 7777
    assert [
        key
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
        if key in kwargs
    ] == ["max_completion_tokens"]
    assert not set(("max_tokens", "max_completion_tokens", "max_output_tokens")) & set(
        kwargs.get("extra_body", {})
    )


@pytest.mark.parametrize(
    "override_key", ["max_tokens", "max_completion_tokens", "max_output_tokens"]
)
@pytest.mark.parametrize("profile", [get_provider_profile("custom"), None])
def test_positive_override_alias_uses_supported_provider_parameter(
    override_key, profile
):
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=profile,
        is_custom_provider=profile is None,
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_completion_tokens": value},
        request_overrides={override_key: 8888},
    )

    assert kwargs["max_completion_tokens"] == 8888
    assert "max_output_tokens" not in kwargs
    assert "max_tokens" not in kwargs


@pytest.mark.parametrize("profile", [get_provider_profile("custom"), None])
def test_top_level_override_wins_mixed_extra_body_collision(profile):
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=profile,
        is_custom_provider=profile is None,
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
        request_overrides={
            "extra_body": {"max_tokens": 7777},
            "max_tokens": 8888,
        },
    )

    assert kwargs["max_tokens"] == 8888
    assert "max_tokens" not in kwargs.get("extra_body", {})


@pytest.mark.parametrize("profile", [get_provider_profile("custom"), None])
def test_top_level_none_does_not_suppress_nested_positive_limit(profile):
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="qwen-local",
        messages=_messages(),
        provider_profile=profile,
        is_custom_provider=profile is None,
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
        request_overrides={
            "extra_body": {"max_tokens": 7777},
            "max_tokens": None,
        },
    )

    assert kwargs["max_tokens"] == 7777
    assert "max_tokens" not in kwargs.get("extra_body", {})


def test_negative_agent_limit_is_classified_and_normalized(tmp_path, monkeypatch):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=-1)

    kwargs = agent._build_api_kwargs(_messages())

    assert agent.max_tokens is None
    assert agent._max_tokens_classification == "invalid_non_positive"
    assert kwargs["max_tokens"] == 4096


@pytest.mark.parametrize("configured", [1, 4096, 12_345])
def test_explicit_positive_agent_limit_is_unchanged(tmp_path, monkeypatch, configured):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=configured)

    kwargs = agent._build_api_kwargs(_messages())

    assert agent.max_tokens == configured
    assert agent._max_tokens_classification == "explicit_positive"
    assert kwargs["max_tokens"] == configured


def test_hosted_provider_behavior_is_unchanged():
    transport = ChatCompletionsTransport()
    profile = get_provider_profile("deepseek")

    omitted = transport.build_kwargs(
        model="deepseek-chat",
        messages=_messages(),
        provider_profile=profile,
        max_tokens=None,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )
    explicit = transport.build_kwargs(
        model="deepseek-chat",
        messages=_messages(),
        provider_profile=profile,
        max_tokens=7777,
        max_tokens_param_fn=lambda value: {"max_tokens": value},
    )

    assert "max_tokens" not in omitted
    assert explicit["max_tokens"] == 7777


@pytest.mark.parametrize(
    ("platform", "session_id"),
    [("desktop", "resumed-desktop-session"), ("cron", "cron-session")],
)
def test_resumed_desktop_and_cron_requests_remain_bounded(
    tmp_path, monkeypatch, platform, session_id
):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=None, platform=platform)
    agent.session_id = session_id

    first = agent._build_api_kwargs(_messages())
    resumed_history = [
        {"role": "user", "content": "earlier turn"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "resumed turn"},
    ]
    resumed_or_next_turn = agent._build_api_kwargs(resumed_history)

    assert first["max_tokens"] == 4096
    assert resumed_or_next_turn["max_tokens"] == 4096


def test_ordinary_default_does_not_change_compression_budget(tmp_path, monkeypatch):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=None)

    assert agent._build_api_kwargs(_messages())["max_tokens"] == 4096
    assert agent.context_compressor.max_tokens is None
    assert agent.context_compressor._coerce_max_tokens(65_536) == 65_536


def _response(content, finish_reason="stop", tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        model="qwen-local",
        usage=None,
    )


def test_tool_turn_keeps_budget_without_duplicate_execution(tmp_path, monkeypatch):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=None, platform="api_server")
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    agent._cached_system_prompt = "stable test prompt"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tools = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    agent.valid_tool_names = ["web_search"]
    tool_call = SimpleNamespace(
        id="call-1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments="{}"),
    )
    agent.client.chat.completions.create.side_effect = [
        _response("", "tool_calls", [tool_call]),
        _response("done"),
    ]

    with (
        patch(
            "run_agent.handle_function_call", return_value="search result"
        ) as execute_tool,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("search")

    assert result["final_response"] == "done"
    assert execute_tool.call_count == 1
    assert agent.client.chat.completions.create.call_count == 2
    assert [
        call.kwargs["max_tokens"]
        for call in agent.client.chat.completions.create.call_args_list
    ] == [4096, 4096]


def test_existing_four_truncation_limit_is_unchanged(tmp_path, monkeypatch):
    agent = _custom_agent(tmp_path, monkeypatch, max_tokens=None)
    agent._cached_system_prompt = "stable test prompt"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.client.chat.completions.create.side_effect = [
        _response(f"part-{index}", "length") for index in range(8)
    ]

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("generate")

    assert agent.client.chat.completions.create.call_count == 4
    assert result["partial"] is True
    assert (
        result["error"] == "Response remained truncated after 4 continuation attempts"
    )
    assert result["final_response"] == "part-0part-1part-2part-3"
