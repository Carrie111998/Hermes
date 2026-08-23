"""Unit tests for pre_llm_call runtime_override (issue #23739)."""

from __future__ import annotations

import pytest

from agent.runtime_override import (
    RUNTIME_OVERRIDE_KEYS,
    apply_runtime_override,
    validate_runtime_override,
)


# ---------------------------------------------------------------------------
# validate_runtime_override
# ---------------------------------------------------------------------------

class TestValidate:
    def test_full_valid_dict(self):
        ro = validate_runtime_override({
            "model": "gpt-5.6",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "api_mode": "chat_completions",
            "system_prompt": "You are a test.",
        })
        assert ro == {
            "model": "gpt-5.6",
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "api_mode": "chat_completions",
            "system_prompt": "You are a test.",
        }

    def test_empty_dict(self):
        assert validate_runtime_override({}) == {}

    def test_not_a_dict(self):
        # Non-dict runtime_override (e.g. 42) -> warning + {} (never crash).
        assert validate_runtime_override(42) == {}

    def test_unsupported_key_ignored(self):
        ro = validate_runtime_override({"model": "m", "temperature": 0.7})
        assert ro == {"model": "m"}

    def test_invalid_value_type_ignored(self):
        ro = validate_runtime_override({"model": 12345})
        assert ro == {}

    def test_empty_string_ignored(self):
        ro = validate_runtime_override({"model": "", "provider": "  "})
        assert ro == {}

    def test_whitelist_matches_spec(self):
        assert RUNTIME_OVERRIDE_KEYS == frozenset({
            "model", "provider", "base_url", "api_key", "api_mode", "system_prompt",
        })


# ---------------------------------------------------------------------------
# apply_runtime_override (context manager snapshot/restore)
# ---------------------------------------------------------------------------

class _FakeAgent:
    """Minimal stand-in for AIAgent with the attributes the override touches."""

    def __init__(self):
        self.model = "orig-model"
        self.provider = "orig-provider"
        self.api_mode = "chat_completions"
        self.api_key = "orig-key"
        self._base_url = "https://orig.example.com/v1"
        self._base_url_lower = self._base_url.lower()
        self._base_url_hostname = "orig.example.com"
        self._client_kwargs = {"api_key": "orig-key", "base_url": "https://orig.example.com/v1"}
        self._anthropic_api_key = "orig-anthropic-key"
        self._anthropic_base_url = "https://orig.anthropic.example.com"

    @property
    def base_url(self):
        return self._base_url

    @base_url.setter
    def base_url(self, value):
        self._base_url = value
        self._base_url_lower = value.lower()
        self._base_url_hostname = value.split("//", 1)[1].split("/", 1)[0]


class TestApply:
    def test_apply_and_restore(self):
        agent = _FakeAgent()
        with apply_runtime_override(agent, {
            "model": "new-model",
            "provider": "new-provider",
            "base_url": "https://new.example.com/v1",
            "api_key": "new-key",
            "api_mode": "anthropic_messages",
        }):
            assert agent.model == "new-model"
            assert agent.provider == "new-provider"
            assert agent.base_url == "https://new.example.com/v1"
            assert agent._base_url_hostname == "new.example.com"
            assert agent.api_key == "new-key"
            assert agent.api_mode == "anthropic_messages"
            assert agent._client_kwargs["api_key"] == "new-key"
            assert agent._client_kwargs["base_url"] == "https://new.example.com/v1"
            assert agent._anthropic_api_key == "new-key"
            assert agent._anthropic_base_url == "https://new.example.com/v1"
        # Restored on exit.
        assert agent.model == "orig-model"
        assert agent.provider == "orig-provider"
        assert agent.base_url == "https://orig.example.com/v1"
        assert agent.api_key == "orig-key"
        assert agent.api_mode == "chat_completions"
        assert agent._client_kwargs == {"api_key": "orig-key", "base_url": "https://orig.example.com/v1"}
        assert agent._anthropic_api_key == "orig-anthropic-key"

    def test_restore_on_exception(self):
        agent = _FakeAgent()
        with pytest.raises(RuntimeError):
            with apply_runtime_override(agent, {"model": "new-model"}):
                assert agent.model == "new-model"
                raise RuntimeError("boom")
        assert agent.model == "orig-model"

    def test_bare_agent_not_polluted(self):
        # Agent created via __new__ has NO attributes; entering the scope must
        # not manufacture attributes on the agent that survive the exit.
        agent = object.__new__(_FakeAgent)
        with apply_runtime_override(agent, {"model": "m", "api_key": "k"}):
            assert agent.model == "m"
        assert not hasattr(agent, "model")
        assert not hasattr(agent, "_client_kwargs")

    def test_partial_override_only_changes_given_keys(self):
        agent = _FakeAgent()
        with apply_runtime_override(agent, {"model": "only-model"}):
            assert agent.model == "only-model"
            assert agent.provider == "orig-provider"  # untouched
            assert agent.api_mode == "chat_completions"  # untouched
