"""Tests for per-model reasoning_effort override during /model switch.

Tests that switch_model:
1. Re-resolves reasoning_config when switching to a model with an override
2. Falls back to global when switching to a model without an override
3. Saves reasoning_config into _primary_runtime for fallback recovery
"""

import pytest
from unittest.mock import MagicMock, patch


class TestSwitchModelReasoningOverride:
    """Test switch_model re-resolves reasoning_config on model switch."""

    def _make_fake_agent(self, model="gpt-5", provider="openai"):
        """Create a minimal fake agent for switch_model testing."""
        agent = MagicMock()
        agent.model = model
        agent.provider = provider
        agent.base_url = "https://api.openai.com/v1"
        agent.api_mode = "openai"
        agent.api_key = "test-key"
        agent._client_kwargs = {"api_key": "test-key", "base_url": "https://api.openai.com/v1"}
        agent._use_prompt_caching = False
        agent._use_native_cache_layout = False
        agent.reasoning_config = None
        # ^ None by default — in production it's set from config via
        # resolve_reasoning_config() at agent init.  Tests that rely on a
        # specific prior value set it explicitly, and the session-override
        # preservation test sets it to a non-config value to simulate
        # /reasoning --session.  See Closes #72856.
        agent._fallback_activated = False
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._config_context_length = None
        agent._transport_cache = {}
        agent.context_compressor = None
        agent._cached_system_prompt = None
        agent._anthropic_api_key = ""
        agent._anthropic_base_url = None
        agent._is_anthropic_oauth = False
        agent._anthropic_prompt_cache_policy = MagicMock(
            return_value=(False, False)
        )
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        return agent

    def test_primary_runtime_includes_reasoning_config(self):
        """After switch_model, _primary_runtime should contain reasoning_config key."""
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()

        fake_cfg = {
            "model": {"default": "claude-opus-4.5"},
            "agent": {
                "reasoning_effort": "medium",
                "reasoning_overrides": {
                    "claude-opus-4.5": "xhigh",
                },
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="claude-opus-4.5",
                    new_provider="anthropic",
                    base_url="https://api.anthropic.com",
                    api_mode="anthropic_messages",
                )
            except Exception:
                # Client creation may fail in test env; check _primary_runtime was set
                pass

        assert hasattr(agent, "_primary_runtime")
        assert "reasoning_config" in agent._primary_runtime

    def test_reasoning_config_resolves_to_override_on_switch(self):
        """switch_model should resolve reasoning_config to per-model override."""
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()

        fake_cfg = {
            "model": {"default": "claude-opus-4.5"},
            "agent": {
                "reasoning_effort": "medium",
                "reasoning_overrides": {
                    "claude-opus-4.5": "xhigh",
                },
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="claude-opus-4.5",
                    new_provider="anthropic",
                    base_url="https://api.anthropic.com",
                    api_mode="anthropic_messages",
                )
            except Exception:
                pass

        # reasoning_config should be updated to xhigh
        assert agent.reasoning_config is not None
        assert agent.reasoning_config.get("effort") == "xhigh"

    def test_reasoning_config_falls_back_to_global(self):
        """switch_model should fall back to global when no override for new model."""
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()

        fake_cfg = {
            "model": {"default": "gpt-5"},
            "agent": {
                "reasoning_effort": "low",
                "reasoning_overrides": {
                    "claude-opus-4.5": "xhigh",  # override for different model
                },
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="gpt-5",
                    new_provider="openai",
                    api_mode="openai",
                )
            except Exception:
                pass

        # No override for gpt-5 → should fall back to global "low"
        assert agent.reasoning_config is not None
        assert agent.reasoning_config.get("effort") == "low"

    def test_restore_primary_runtime_restores_reasoning(self):
        """restore_primary_runtime should restore reasoning_config from snapshot."""
        from agent.agent_runtime_helpers import restore_primary_runtime

        agent = MagicMock()
        agent._primary_runtime = {
            "model": "claude-opus-4.5",
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_mode": "anthropic_messages",
            "api_key": "key",
            "client_kwargs": {},
            "use_prompt_caching": True,
            "use_native_cache_layout": False,
            "reasoning_config": {"enabled": True, "effort": "xhigh"},
            "compressor_model": "claude-opus-4.5",
            "compressor_base_url": "",
            "compressor_api_key": "",
            "compressor_provider": "",
            "compressor_context_length": 0,
            "compressor_api_mode": "",
            "compressor_threshold_tokens": 0,
            "anthropic_api_key": "key",
            "anthropic_base_url": "https://api.anthropic.com",
            "is_anthropic_oauth": False,
        }
        agent._fallback_activated = True
        agent._fallback_index = 0
        agent._fallback_chain = []
        agent._fallback_model = None
        agent._transport_cache = {}
        agent._config_context_length = None
        agent._rate_limited_until = 0
        agent.model = "fallback-model"
        agent.provider = "openai"
        agent.reasoning_config = {"enabled": True, "effort": "medium"}
        agent.context_compressor = MagicMock()
        agent.base_url = ""
        # Mock the methods restore_primary_runtime calls
        agent._anthropic_prompt_cache_policy = MagicMock(return_value=(True, False))
        agent._create_openai_client = MagicMock(return_value=MagicMock())
        agent._ensure_lmstudio_runtime_loaded = MagicMock()

        result = restore_primary_runtime(agent)
        assert result is True
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_switch_model_global_fallback_with_yaml_false(self):
        """switch_model global fallback must not coerce YAML boolean False.

        Regression: str(... or "").strip() turned False into "", silently
        re-enabling thinking. The raw value must pass through so
        parse_reasoning_effort(False) returns {'enabled': False}.
        """
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()

        fake_cfg = {
            "model": {"default": "gpt-5"},
            "agent": {
                "reasoning_effort": False,  # YAML boolean, not string
                "reasoning_overrides": {},
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="gpt-5",
                    new_provider="openai",
                    api_mode="openai",
                )
            except Exception:
                pass

        # No override for gpt-5 → global fallback with raw False
        assert agent.reasoning_config is not None
        assert agent.reasoning_config.get("enabled") is False

    def test_session_reasoning_override_preserved_on_switch(self):
        """Session-level reasoning_effort must survive /model switch (Closes #72856).

        When the caller has applied a session override (simulated by setting
        reasoning_config to a non-config value), switch_model must preserve
        it rather than re-resolving from config alone.
        """
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent(model="sonnet-4.6", provider="anthropic")

        # Simulate a session-level override (as CLI /reasoning high or gateway
        # /reasoning --session would set).
        agent.reasoning_config = {"enabled": True, "effort": "xhigh"}

        fake_cfg = {
            "model": {"default": "sonnet-4.6"},
            "agent": {
                "reasoning_effort": "medium",  # global differs from session override
                "reasoning_overrides": {},
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="gpt-5",  # switch to a different model
                    new_provider="openai",
                    base_url="https://api.openai.com/v1",
                    api_mode="openai",
                )
            except Exception:
                pass

        # Session override must be preserved — config says "medium"/gpt-5 would
        # resolve to "medium" (no override), but the prior "xhigh" was a session
        # override and should survive.
        assert agent.reasoning_config is not None
        assert agent.reasoning_config.get("effort") == "xhigh", (
            f"Expected session override 'xhigh' to be preserved after model "
            f"switch, got {agent.reasoning_config}"
        )