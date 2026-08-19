"""End-to-end CLI coverage for named custom-provider vision routing.

Adapted from the independently reproduced CLI regression in #69896.  Unlike
that PR, these tests keep the live transport canonical (``provider=custom``)
and assert that the separate requested identity reaches each capability gate.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

from run_agent import AIAgent as RealAIAgent
from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin


MODEL = "qwen3.8-max-preview"
REQUESTED_PROVIDER = "custom:qwen-token-plan"


def _install_real_agent_probe_guards(monkeypatch):
    context_length = MagicMock(return_value=262144)
    endpoint_metadata = MagicMock(side_effect=AssertionError("constructor attempted endpoint metadata access"))
    local_server = MagicMock(side_effect=AssertionError("constructor attempted local-server detection"))
    monkeypatch.setattr("agent.context_compressor.get_model_context_length", context_length)
    monkeypatch.setattr("agent.model_metadata.fetch_endpoint_model_metadata", endpoint_metadata)
    monkeypatch.setattr("agent.model_metadata.detect_local_server_type", local_server)
    return context_length, endpoint_metadata, local_server


def _assert_real_agent_probe_guards(probes):
    context_length, endpoint_metadata, local_server = probes
    context_length.assert_called()
    endpoint_metadata.assert_not_called()
    local_server.assert_not_called()


def _install_real_agent_factory(monkeypatch, target, captured):
    captured["constructor_probes"] = _install_real_agent_probe_guards(monkeypatch)

    def construct(*args, **kwargs):
        captured["args"] = deepcopy(args)
        captured["kwargs"] = deepcopy(kwargs)
        agent = RealAIAgent(*args, **kwargs)
        captured["agent"] = agent
        agent.run_conversation = MagicMock(return_value={"final_response": "synthetic-response", "messages": []})
        agent.chat = MagicMock(return_value="synthetic-response")
        return agent

    monkeypatch.setattr(target, construct)


class _RuntimeCLI(CLIAgentSetupMixin):
    def __init__(self, *, model: str, provider: str):
        self.model = model
        self.requested_provider = provider
        self.provider = provider
        self.api_key = None
        self.base_url = None
        self.api_mode = "chat_completions"
        self.acp_command = None
        self.acp_args = []
        self.agent = None
        self._fallback_model = []
        self._explicit_api_key = None
        self._explicit_base_url = None
        self._credential_pool = None
        self.service_tier = None

    def _normalize_model_for_provider(self, _provider: str) -> bool:
        return False


def _write_profile_config(hermes_home) -> None:
    (hermes_home / "config.yaml").write_text(
        """
model:
  default: ollama-cloud/glm-5.2
  provider: ollama-cloud
providers:
  qwen-token-plan:
    base_url: https://qwen-token-plan.example/v1
    api_key: test-key
    models:
      qwen3.8-max-preview:
        supports_vision: true
agent:
  image_input_mode: auto
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _resolve_cli_route():
    from hermes_cli._parser import build_top_level_parser
    from hermes_constants import get_hermes_home

    _write_profile_config(get_hermes_home())
    parser, _subparsers, _chat = build_top_level_parser()
    args, _unknown = parser.parse_known_args(
        ["-m", MODEL, "--provider", REQUESTED_PROVIDER, "chat"]
    )
    cli = _RuntimeCLI(model=args.model, provider=args.provider)
    assert cli._ensure_runtime_credentials() is True
    return cli, cli._resolve_turn_agent_config("inspect the image")


def test_real_cli_args_keep_transport_and_capability_identities_separate():
    from agent.image_routing import decide_image_input_mode
    from hermes_cli.config import load_config

    cli, route = _resolve_cli_route()
    runtime = route["runtime"]

    assert cli.provider == "custom"
    assert cli.requested_provider == REQUESTED_PROVIDER
    assert runtime["provider"] == "custom"
    assert runtime["requested_provider"] == REQUESTED_PROVIDER
    assert decide_image_input_mode(
        runtime["provider"],
        route["model"],
        load_config(),
        requested_provider=runtime["requested_provider"],
    ) == "native"

    # A credential refresh with the same two identities must retain the live
    # agent instead of treating canonicalization as a provider switch.
    sentinel_agent = SimpleNamespace()
    cli.agent = sentinel_agent
    assert cli._ensure_runtime_credentials() is True
    assert cli.agent is sentinel_agent


def test_named_identity_reaches_agent_and_vision_tool_native_gates():
    from agent.auxiliary_client import reset_runtime_main, set_runtime_main
    from run_agent import AIAgent
    from tools.vision_tools import _should_use_native_vision_fast_path

    _cli, route = _resolve_cli_route()
    runtime = route["runtime"]
    token = set_runtime_main(
        runtime["provider"],
        route["model"],
        requested_provider=runtime["requested_provider"],
        base_url=runtime["base_url"],
        api_key=runtime["api_key"],
        api_mode=runtime["api_mode"],
    )
    try:
        agent = AIAgent.__new__(AIAgent)
        agent.provider = runtime["provider"]
        agent.requested_provider = runtime["requested_provider"]
        agent.model = route["model"]

        assert agent._model_supports_vision() is True
        assert _should_use_native_vision_fast_path() is True
    finally:
        reset_runtime_main(token)


def test_cli_primary_real_agent_keeps_provider_layer(tmp_path, monkeypatch):
    import cli

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    runtime = {
        "provider": "custom", "requested_provider": "ollama",
        "api_key": "synthetic-key", "base_url": "http://127.0.0.1:11434/v1",
        "api_mode": "chat_completions", "request_overrides": {},
        "credential_pool": None, "command": "synthetic-command", "args": ["--synthetic"],
        "max_output_tokens": None,
    }
    cli_instance = _RuntimeCLI(model="synthetic-model", provider="ollama")
    cli_instance.__dict__.update({
        "max_tokens": 4096, "max_turns": 2, "enabled_toolsets": [],
        "disabled_toolsets": [], "verbose": False, "tool_progress_mode": "all",
        "system_prompt": "", "prefill_messages": [], "reasoning_config": None,
        "_providers_only": None, "_providers_ignore": None, "_providers_order": None,
        "_provider_sort": None, "_provider_require_params": False,
        "_provider_data_collection": None, "_openrouter_min_coding_score": None,
        "session_id": "synthetic-session", "_session_db": MagicMock(),
        "_resumed": False, "conversation_history": [],
        "checkpoints_enabled": False, "checkpoint_max_snapshots": 5,
        "checkpoint_max_total_size_mb": 50, "checkpoint_max_file_size_mb": 10,
        "pass_session_id": False, "ignore_rules": True, "streaming_enabled": False,
        "_inline_diffs_enabled": False, "_pending_title": None,
    })
    cli_instance._clarify_callback = None
    cli_instance.finalize_preloaded_skills = lambda: None
    cli_instance._install_tool_callbacks = lambda: None
    cli_instance._ensure_tirith_security = lambda: None
    cli_instance._ensure_runtime_credentials = lambda: True
    cli_instance._current_reasoning_callback = lambda: None
    cli_instance._on_thinking = None
    cli_instance._on_tool_progress = None
    cli_instance._on_tool_start = None
    cli_instance._on_tool_complete = None
    cli_instance._stream_delta = None
    cli_instance._on_tool_gen_start = None
    cli_instance._on_notice = None
    cli_instance._on_notice_clear = None
    cli_instance._on_reaction = None
    captured = {}
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))
    _install_real_agent_factory(monkeypatch, "cli.AIAgent", captured)
    monkeypatch.setattr("agent.credits_tracker.seed_credits_at_session_start", lambda *_a: None)

    assert cli_instance._init_agent(runtime_override=runtime) is True
    assert isinstance(captured["agent"], RealAIAgent)
    assert captured["kwargs"]["requested_provider"] == "ollama"
    assert captured["kwargs"]["provider_request_overrides"] == {}
    assert (cli_instance.agent.provider, cli_instance.agent.requested_provider) == ("custom", "ollama")
    assert cli_instance.agent._caller_request_overrides == {}
    assert cli_instance.agent._provider_request_overrides == {}
    _assert_real_agent_probe_guards(captured["constructor_probes"])
