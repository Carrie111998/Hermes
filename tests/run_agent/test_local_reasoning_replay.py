"""Regression test: config-driven reasoning_content echo for self-hosted thinking models.

Kimi K3 served locally (llama.cpp / any OpenAI-compatible endpoint) has the
same replay requirement as the hosted Kimi/Moonshot API: the full assistant
message, including ``reasoning_content``, must be passed back every turn.
Unlike the hosted APIs nothing fails loudly when it is omitted — the history
just renders without thinking blocks and agentic quality silently degrades.

Host-based detection (``_needs_kimi_tool_reasoning``) can never match a
self-hosted endpoint, and model-name matching was deliberately rejected
because aggregators re-exporting Kimi models 422 on the echo. The fix is an
explicit per-provider opt-in::

    providers:
      llamacpp:
        base_url: http://127.0.0.1:8091/v1
        reasoning_replay: true

Covered here:

1. Without the flag, a local provider still strips ``reasoning_content`` on
   replay (the safe cross-provider default is preserved).
2. With the flag, ``copy_reasoning_content_for_api`` preserves reasoning
   verbatim and upgrades legacy ``""`` pads to ``" "``, identical to the
   hosted Kimi/DeepSeek behavior.
3. The flag only affects the named provider; other providers are untouched.
"""

from __future__ import annotations

import pytest

from agent.agent_runtime_helpers import copy_reasoning_content_for_api
from run_agent import AIAgent


def _make_agent(provider: str = "llamacpp", model: str = "kimi-k3",
                base_url: str = "http://127.0.0.1:8091/v1") -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.verbose_logging = False
    return agent


def _patch_config(monkeypatch, providers: dict) -> None:
    import run_agent as run_agent_mod

    def fake_load_config():
        return {"providers": providers}

    import hermes_cli.config as config_mod
    monkeypatch.setattr(config_mod, "load_config_readonly", fake_load_config)


def _replay(agent, source_msg: dict) -> dict:
    api_msg = dict(source_msg)
    copy_reasoning_content_for_api(agent, source_msg, api_msg)
    return api_msg


class TestProviderReasoningReplayConfigured:
    def test_flag_absent_returns_false(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"base_url": "http://127.0.0.1:8091/v1"}})
        agent = _make_agent()
        assert agent._provider_reasoning_replay_configured() is False

    def test_flag_true_returns_true(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent()
        assert agent._provider_reasoning_replay_configured() is True

    def test_other_provider_unaffected(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent(provider="ollama", base_url="http://localhost:11434/v1")
        assert agent._provider_reasoning_replay_configured() is False

    def test_no_provider_returns_false(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {})
        agent = _make_agent(provider="")
        assert agent._provider_reasoning_replay_configured() is False

    def test_feeds_thinking_pad_gate(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent()
        assert agent._needs_thinking_reasoning_pad() is True


class TestLocalReplayStripAndPreserve:
    SOURCE = {
        "role": "assistant",
        "content": "calling a tool",
        "reasoning_content": "the model's chain of thought",
    }

    def test_without_flag_reasoning_is_stripped(self, monkeypatch) -> None:
        """Documents the pre-existing safe default for unconfigured locals."""
        _patch_config(monkeypatch, {"llamacpp": {"base_url": "http://127.0.0.1:8091/v1"}})
        agent = _make_agent()
        api_msg = _replay(agent, dict(self.SOURCE))
        assert "reasoning_content" not in api_msg

    def test_with_flag_reasoning_preserved_verbatim(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent()
        api_msg = _replay(agent, dict(self.SOURCE))
        assert api_msg["reasoning_content"] == "the model's chain of thought"

    def test_with_flag_empty_pad_upgraded_to_space(self, monkeypatch) -> None:
        """Legacy empty-string pads must upgrade to " " like hosted providers."""
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent()
        src = dict(self.SOURCE)
        src["reasoning_content"] = ""
        api_msg = _replay(agent, src)
        assert api_msg["reasoning_content"] == " "

    def test_non_assistant_untouched(self, monkeypatch) -> None:
        _patch_config(monkeypatch, {"llamacpp": {"reasoning_replay": True}})
        agent = _make_agent()
        src = {"role": "user", "content": "hi", "reasoning_content": "junk"}
        api_msg = _replay(agent, src)
        assert api_msg["reasoning_content"] == "junk"
