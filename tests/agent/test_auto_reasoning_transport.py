import types
from unittest.mock import patch

from agent.chat_completion_helpers import build_api_kwargs, handle_max_iterations


class _CapturingTransport:
    def __init__(self):
        self.kwargs = None

    def build_kwargs(self, **kwargs):
        self.kwargs = kwargs
        return kwargs


class _GitHubResponsesAgent:
    tools = []
    api_mode = "codex_responses"
    model = "gpt-5.4"
    base_url = "https://models.github.ai/inference"
    provider = "github-models"
    _base_url_hostname = "models.github.ai"
    _base_url_lower = base_url.lower()
    reasoning_config = {"enabled": True, "effort": "auto"}
    session_id = "session-auto"
    max_tokens = 4096
    request_overrides = {}
    _codex_reasoning_replay_enabled = True
    codex_responses_native_compaction = False

    def __init__(self):
        self.transport = _CapturingTransport()

    def _get_transport(self):
        return self.transport

    def _prepare_messages_for_non_vision_model(self, messages):
        return messages

    def _resolved_api_call_timeout(self):
        return 30

    def _github_models_reasoning_extra_body(self, reasoning_config=None):
        config = self.reasoning_config if reasoning_config is None else reasoning_config
        return {"effort": config["effort"]}


def test_github_responses_transport_uses_resolved_auto_reasoning_config():
    agent = _GitHubResponsesAgent()

    build_api_kwargs(
        agent,
        [{"role": "user", "content": "debug this production security incident"}],
    )

    assert agent.transport.kwargs is not None
    assert agent.transport.kwargs["reasoning_config"] == {
        "enabled": True,
        "effort": "high",
    }
    assert agent.transport.kwargs["github_reasoning_extra"] == {"effort": "high"}


def test_lmstudio_iteration_summary_uses_resolved_auto_reasoning_config():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="lm-studio",
        provider="lmstudio",
        model="reasoning-model",
        base_url="http://localhost:1234/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        reasoning_config={"enabled": True, "effort": "auto"},
    )
    setattr(agent, "_cached_system_prompt", "SYS")

    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "RAW-RESPONSE"

    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions())
    )
    transport = types.SimpleNamespace(
        normalize_response=lambda _response: types.SimpleNamespace(content="SUMMARY")
    )
    messages = [
        {"role": "user", "content": "debug this production security incident"},
        {"role": "assistant", "content": "working"},
    ]

    with (
        patch.object(agent, "_ensure_primary_openai_client", return_value=client),
        patch.object(agent, "_get_transport", return_value=transport),
        patch.object(agent, "_supports_reasoning_extra_body", return_value=True),
        patch.object(
            agent,
            "_lmstudio_reasoning_options_cached",
            return_value=["low", "medium", "high"],
        ),
    ):
        result = handle_max_iterations(agent, messages, 5)

    assert result == "SUMMARY"
    assert captured["reasoning_effort"] == "high"
