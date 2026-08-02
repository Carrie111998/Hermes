from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import agent.speculative_compression as speculative
from agent.speculative_compression import (
    configure_speculative_compression,
    reset_speculative_compression_to_config,
    schedule_tool_wait_candidate,
    speculative_compression_status,
)


class Compressor:
    context_length = 1_000
    max_tokens = 100

    def _protect_head_size(self, _messages):
        return 0

    def _find_tail_cut_by_tokens(self, messages, _head_end):
        return len(messages) - 1

    def _align_boundary_forward(self, _messages, cut):
        return cut


class Manager:
    def __init__(self):
        self.started = []
        self.invalidated = []

    def maybe_start(self, session_id, snapshot, factory, **kwargs):
        self.started.append((session_id, snapshot, factory, kwargs))
        return "started"

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)


def test_runtime_switch_wires_scheduling_and_invalidates(monkeypatch):
    manager = Manager()
    agent = SimpleNamespace(
        compression_enabled=True,
        speculative_compression_enabled=False,
        context_compressor=Compressor(),
        api_mode="chat_completions",
        session_id="session-1",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: manager)
    monkeypatch.setattr(
        speculative,
        "is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )

    assert configure_speculative_compression(agent, True) is True
    assert agent.speculative_compression_enabled is True
    assert agent.speculative_compression_settings.enabled is True
    assert agent._speculative_compression_manager is manager
    assert agent._speculative_runtime_override is True
    assert "enabled=True" in speculative_compression_status(agent)
    assert "eligible=True" in speculative_compression_status(agent)
    assert "api_mode=chat_completions" in speculative_compression_status(agent)
    assert "context_engine=Compressor" in speculative_compression_status(agent)

    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "current"},
    ]
    assert schedule_tool_wait_candidate(agent, messages, 800) == "started"
    assert len(manager.started) == 1

    assert configure_speculative_compression(agent, False) is False
    assert agent.speculative_compression_enabled is False
    assert manager.invalidated == ["session-1"]
    assert agent._speculative_runtime_override is False
    assert schedule_tool_wait_candidate(agent, messages, 800) == "disabled"
    assert len(manager.started) == 1


def test_runtime_override_is_cleared_when_config_wiring_is_rebuilt(monkeypatch):
    manager = Manager()
    agent = SimpleNamespace(
        compression_enabled=True,
        speculative_compression_enabled=False,
        context_compressor=Compressor(),
        api_mode="chat_completions",
        session_id="session-1",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: manager)
    monkeypatch.setattr(
        speculative,
        "is_builtin_compression_eligible",
        lambda **_kwargs: True,
    )

    assert configure_speculative_compression(agent, True) is True
    assert reset_speculative_compression_to_config(agent) is False
    assert agent._speculative_runtime_override is None
    assert agent.speculative_compression_enabled is False
    assert agent.speculative_compression_settings.enabled is False
    assert agent._speculative_compression_manager is None


def test_enable_reports_compression_disabled_blocker(monkeypatch):
    agent = SimpleNamespace(
        compression_enabled=False,
        context_compressor=Compressor(),
        api_mode="chat_completions",
        session_id="session-1",
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "is_builtin_compression_eligible", lambda **_kwargs: True)

    assert configure_speculative_compression(agent, True) is False
    status = speculative_compression_status(agent)
    assert "enabled=False" in status
    assert "blocker=compression_disabled" in status
    assert "runtime_override=True" in status


def test_enable_reports_codex_and_plugin_blockers(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: Manager())

    codex_agent = SimpleNamespace(
        compression_enabled=True,
        context_compressor=Compressor(),
        api_mode="codex_app_server",
        session_id="codex-session",
    )
    assert configure_speculative_compression(codex_agent, True) is False
    assert "blocker=codex_app_server" in speculative_compression_status(codex_agent)

    plugin_agent = SimpleNamespace(
        compression_enabled=True,
        context_compressor=SimpleNamespace(),
        api_mode="chat_completions",
        session_id="plugin-session",
    )
    assert configure_speculative_compression(plugin_agent, True) is False
    assert "blocker=context_engine_not_builtin" in speculative_compression_status(plugin_agent)


def test_model_switch_clears_runtime_override_and_reloads_config(monkeypatch):
    from agent.agent_runtime_helpers import switch_model
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.compression_enabled = True
    agent._speculative_runtime_override = True
    agent.speculative_compression_enabled = True
    agent._speculative_compression_manager = object()
    agent._create_openai_client = lambda *_args, **_kwargs: object()
    agent._apply_client_headers_for_base_url = lambda *_args, **_kwargs: None
    agent._ensure_lmstudio_runtime_loaded = lambda *_args, **_kwargs: None
    agent._lmstudio_load_was_unverified = lambda *_args, **_kwargs: False
    agent._effective_lmstudio_context_length = lambda configured, runtime: configured or runtime or 1_000
    agent._anthropic_prompt_cache_policy = lambda **_kwargs: (False, False)
    agent.context_compressor.update_model = lambda **_kwargs: None
    agent._fallback_chain = []
    agent.reasoning_config = {}

    monkeypatch.setattr("agent.model_metadata.get_model_context_length", lambda *args, **kwargs: 1_000)
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agent.chat_completion_helpers._reset_stale_streak", lambda *_args: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"compression": {"speculative": {"enabled": False}}},
    )
    monkeypatch.setattr(speculative, "get_default_manager", lambda: Manager())

    switch_model(
        agent,
        "test/next-model",
        agent.provider,
        api_key="test-key",
        base_url=agent.base_url,
        api_mode="chat_completions",
    )

    assert agent._speculative_runtime_override is None
    assert agent.speculative_compression_enabled is False
    assert agent._speculative_compression_manager is None
