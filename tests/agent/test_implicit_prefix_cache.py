"""Tests for implicit-prefix-cache backend handling.

On local backends (llama.cpp, Ollama, LM Studio, vLLM) that implement
longest-common-prefix KV-cache matching, any byte change anywhere in the
prompt invalidates the cache for everything after it. For these backends,
the volatile suffix (model/provider/timestamp) is stored separately and
injected AFTER conversation history so a model switch only invalidates a
small trailing region.
"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from agent.system_prompt import build_system_prompt, build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="qwen3.8-27b",
        provider="custom",
        platform="",
        pass_session_id=False,
        session_id="",
        _use_prompt_caching=False,
        _use_native_cache_layout=False,
        _implicit_prefix_cache=False,
        _volatile_suffix=None,
        base_url="http://localhost:8080/v1",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestImplicitPrefixCacheDetection:
    """_implicit_prefix_cache is set when the endpoint is local and not using
    explicit cache_control markers."""

    def test_local_endpoint_without_caching_is_implicit_prefix_cache(self):
        agent = _make_agent(
            _use_prompt_caching=False,
            base_url="http://localhost:8080/v1",
        )
        from agent.model_metadata import is_local_endpoint
        assert is_local_endpoint(agent.base_url)
        # After init, _implicit_prefix_cache should be True
        # (set by agent_init.py logic)
        agent._implicit_prefix_cache = (
            not agent._use_prompt_caching
            and is_local_endpoint(agent.base_url)
        )
        assert agent._implicit_prefix_cache is True

    def test_cloud_endpoint_is_not_implicit_prefix_cache(self):
        agent = _make_agent(
            _use_prompt_caching=True,
            base_url="https://api.openai.com/v1",
        )
        from agent.model_metadata import is_local_endpoint
        agent._implicit_prefix_cache = (
            not agent._use_prompt_caching
            and is_local_endpoint(agent.base_url)
        )
        assert agent._implicit_prefix_cache is False

    def test_local_endpoint_with_explicit_caching_is_not_implicit(self):
        agent = _make_agent(
            _use_prompt_caching=True,
            base_url="http://localhost:8080/v1",
        )
        from agent.model_metadata import is_local_endpoint
        agent._implicit_prefix_cache = (
            not agent._use_prompt_caching
            and is_local_endpoint(agent.base_url)
        )
        assert agent._implicit_prefix_cache is False


class TestVolatileSuffixExtraction:
    """For implicit-prefix-cache backends, the volatile suffix is stored
    separately and excluded from the system prompt."""

    def test_volatile_suffix_stored_separately(self):
        agent = _make_agent(
            _implicit_prefix_cache=True,
            model="qwen3.8-27b",
            provider="custom",
        )
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
            patch("hermes_time.now", return_value=datetime(2026, 1, 2)),
        ):
            parts = build_system_prompt_parts(agent)

        # The volatile tier should be empty in the returned parts
        assert parts["volatile"] == ""
        # The volatile suffix should be stored on the agent
        assert agent._volatile_suffix is not None
        assert "Model: qwen3.8-27b" in agent._volatile_suffix
        assert "Provider: custom" in agent._volatile_suffix

    def test_non_implicit_backend_keeps_volatile_in_prompt(self):
        agent = _make_agent(
            _implicit_prefix_cache=False,
            model="claude-4-sonnet",
            provider="anthropic",
        )
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
            patch("hermes_time.now", return_value=datetime(2026, 1, 2)),
        ):
            parts = build_system_prompt_parts(agent)

        # For explicit-cache backends, volatile stays in the prompt
        assert "Model: claude-4-sonnet" in parts["volatile"]
        assert agent._volatile_suffix is None

    def test_system_prompt_excludes_model_line_for_implicit(self):
        agent = _make_agent(
            _implicit_prefix_cache=True,
            model="qwen3.8-27b",
            provider="custom",
        )
        with (
            patch("run_agent.load_soul_md", return_value=""),
            patch("run_agent.build_nous_subscription_prompt", return_value=""),
            patch("run_agent.build_environment_hints", return_value=""),
            patch("run_agent.build_context_files_prompt", return_value=""),
            patch("hermes_time.now", return_value=datetime(2026, 1, 2)),
        ):
            prompt = build_system_prompt(agent)

        # The model line should NOT be in the system prompt
        assert "Model: qwen3.8-27b" not in prompt
        assert "Provider: custom" not in prompt
        # But it should be in the volatile suffix
        assert "Model: qwen3.8-27b" in agent._volatile_suffix


class TestStoredPromptMatchesRuntime:
    """_stored_prompt_matches_runtime should not reject stored prompts for
    implicit-prefix-cache backends just because the model line is no longer
    embedded in the system prompt."""

    def test_implicit_cache_skips_model_mismatch(self):
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = _make_agent(
            _implicit_prefix_cache=True,
            model="new-model",
            provider="custom",
        )
        # Stored prompt from before the fix (no Model line)
        stored_prompt = "Some stable content\n\nConversation started: Friday, January 02, 2026"
        result = _stored_prompt_matches_runtime(agent, stored_prompt)
        assert result is True

    def test_non_implicit_cache_rejects_model_mismatch(self):
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = _make_agent(
            _implicit_prefix_cache=False,
            model="new-model",
            provider="custom",
        )
        stored_prompt = "Some stable content\n\nModel: old-model\nProvider: custom"
        result = _stored_prompt_matches_runtime(agent, stored_prompt)
        assert result is False

    def test_implicit_cache_skips_provider_mismatch(self):
        from agent.conversation_loop import _stored_prompt_matches_runtime

        agent = _make_agent(
            _implicit_prefix_cache=True,
            model="model",
            provider="new-provider",
        )
        stored_prompt = "Some stable content\n\nModel: model\nProvider: old-provider"
        result = _stored_prompt_matches_runtime(agent, stored_prompt)
        assert result is True


class TestVolatileSuffixInjection:
    """The volatile suffix should be injected after conversation history
    for implicit-prefix-cache backends."""

    def test_volatile_suffix_injected_after_history(self):
        agent = _make_agent(
            _implicit_prefix_cache=True,
            model="qwen3.8-27b",
            provider="custom",
            _volatile_suffix="Model: qwen3.8-27b\nProvider: custom",
        )
        # Simulate the injection logic
        api_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        if getattr(agent, "_implicit_prefix_cache", False) and getattr(agent, "_volatile_suffix", None):
            api_messages.append({"role": "user", "content": agent._volatile_suffix})

        assert len(api_messages) == 3
        assert api_messages[-1]["role"] == "user"
        assert "Model: qwen3.8-27b" in api_messages[-1]["content"]

    def test_no_injection_for_non_implicit_backend(self):
        agent = _make_agent(
            _implicit_prefix_cache=False,
            model="claude-4-sonnet",
            provider="anthropic",
            _volatile_suffix=None,
        )
        api_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        if getattr(agent, "_implicit_prefix_cache", False) and getattr(agent, "_volatile_suffix", None):
            api_messages.append({"role": "user", "content": agent._volatile_suffix})

        assert len(api_messages) == 2
