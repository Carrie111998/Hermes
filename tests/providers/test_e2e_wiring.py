"""E2E tests: verify _build_kwargs_from_profile produces correct output.

These tests call _build_kwargs_from_profile on the transport directly,
without importing run_agent (which would cause xdist worker contamination).
"""

import pytest
from agent.transports.chat_completions import ChatCompletionsTransport
from providers import get_provider_profile


@pytest.fixture
def transport():
    return ChatCompletionsTransport()


def _msgs():
    return [{"role": "user", "content": "hi"}]


class TestNvidiaProfileWiring:


    def test_nvidia_model_passed(self, transport):
        profile = get_provider_profile("nvidia")
        kwargs = transport.build_kwargs(
            model="nvidia/test-model",
            messages=_msgs(),
            tools=None,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides=None,
            session_id="test",
            ollama_num_ctx=None,
        )
        assert kwargs["model"] == "nvidia/test-model"


    def test_nvidia_tool_messages_drop_name_fields(self, transport):
        profile = get_provider_profile("nvidia")
        msgs = [
            {"role": "user", "content": "run a command"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "terminal", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "terminal",
                "tool_name": "terminal",
                "tool_call_id": "call_1",
                "content": "ok",
            },
        ]
        kwargs = transport.build_kwargs(
            model="mistralai/mistral-large-3-675b-instruct-2512",
            messages=msgs,
            tools=None,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides=None,
            session_id="test",
            ollama_num_ctx=None,
        )

        assert kwargs["messages"][2] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "ok",
        }
        assert msgs[2]["name"] == "terminal"
        assert msgs[2]["tool_name"] == "terminal"


class TestDeepSeekProfileWiring:
    def test_deepseek_no_forced_max_tokens(self, transport):
        profile = get_provider_profile("deepseek")
        kwargs = transport.build_kwargs(
            model="deepseek-chat",
            messages=_msgs(),
            tools=None,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides=None,
            session_id="test",
            ollama_num_ctx=None,
        )
        # DeepSeek has no default_max_tokens
        assert kwargs["model"] == "deepseek-chat"
        assert kwargs.get("max_tokens") is None or "max_tokens" not in kwargs


class TestCustomProfileWiring:
    """provider=custom: temperature + parallel_tool_calls reach the wire (#18470).

    Local OpenAI-compat backends (llama.cpp, vLLM) default to temperature=1.0
    and refuse to batch tool rounds when the fields are omitted, so the custom
    profile must emit both; request_overrides are merged last and still win.
    """

    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "terminal",
                "description": "run a command",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    def test_custom_sends_temperature_and_parallel_tool_calls(self, transport):
        profile = get_provider_profile("custom")
        kwargs = transport.build_kwargs(
            model="llama3",
            messages=_msgs(),
            tools=self.TOOLS,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides=None,
            session_id="test",
            ollama_num_ctx=None,
        )
        assert kwargs["temperature"] == 0.2
        assert kwargs["parallel_tool_calls"] is True

    def test_custom_parallel_tool_calls_omitted_without_tools(self, transport):
        profile = get_provider_profile("custom")
        kwargs = transport.build_kwargs(
            model="llama3",
            messages=_msgs(),
            tools=None,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides=None,
            session_id="test",
            ollama_num_ctx=None,
        )
        # temperature still pinned; parallel_tool_calls is meaningless without
        # tools and must stay off the wire
        assert kwargs["temperature"] == 0.2
        assert "parallel_tool_calls" not in kwargs

    def test_custom_request_overrides_win(self, transport):
        profile = get_provider_profile("custom")
        kwargs = transport.build_kwargs(
            model="llama3",
            messages=_msgs(),
            tools=self.TOOLS,
            provider_profile=profile,
            max_tokens=None,
            max_tokens_param_fn=lambda x: {"max_tokens": x} if x else {},
            timeout=300,
            reasoning_config=None,
            request_overrides={"temperature": 0.7, "parallel_tool_calls": False},
            session_id="test",
            ollama_num_ctx=None,
        )
        assert kwargs["temperature"] == 0.7
        assert kwargs["parallel_tool_calls"] is False

