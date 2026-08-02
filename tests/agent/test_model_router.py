"""Tests for agent/model_router.py.

Covers Fase 1, Prompt 1.2 acceptance criteria:
  * Changing the model = editing model_config.yaml, not code.
  * select_model() output feeds the *existing* fallback mechanism
    (agent._fallback_chain / try_activate_fallback) correctly -- this
    module does not reimplement retry/timeout/rate-limit handling, so the
    "timeout -> fallback" and "429 -> fallback" tests exercise the real
    AIAgent + try_activate_fallback path, not a reimplementation.
  * Every fallback activation is logged (provider, model, reason).
"""

import logging

import pytest
from unittest.mock import MagicMock, patch

from agent.error_classifier import FailoverReason
from agent.model_router import load_model_config, select_model
from run_agent import AIAgent


REAL_CONFIG_YAML = """
agent_id: "iyari"
profiles:
  cheap:
    provider: openrouter
    model: minimax/minimax-01
    max_tokens: 2048
    timeout: 15
    max_retries: 2
    fallback_chain:
      - provider: openrouter
        model: openai/gpt-4o-mini
  main:
    provider: openrouter
    model: moonshotai/kimi-k2.7-code
    max_tokens: 8192
    timeout: 30
    max_retries: 3
    fallback_chain:
      - provider: openrouter
        model: anthropic/claude-sonnet-4
  premium:
    provider: openrouter
    model: deepseek/deepseek-reasoner
    max_tokens: 16384
    timeout: 60
    max_retries: 3
    fallback_chain:
      - provider: openrouter
        model: anthropic/claude-sonnet-4
"""


def _write_config(tmp_path, text=REAL_CONFIG_YAML):
    path = tmp_path / "model_config.yaml"
    path.write_text(text)
    return str(path)


class TestLoadModelConfig:
    def test_loads_real_shipped_config(self):
        cfg = load_model_config()
        assert set(cfg["profiles"]) >= {"cheap", "main", "premium"}
        assert cfg["agent_id"]

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_model_config(str(tmp_path / "nope.yaml"))

    def test_malformed_yaml_raises(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("profiles: [this is not: valid: yaml")
        with pytest.raises(ValueError):
            load_model_config(str(bad))

    def test_missing_profiles_section_raises(self, tmp_path):
        no_profiles = tmp_path / "no_profiles.yaml"
        no_profiles.write_text("agent_id: iyari\n")
        with pytest.raises(ValueError):
            load_model_config(str(no_profiles))


class TestSelectModel:
    def test_selects_each_profile_from_real_config(self):
        cfg = load_model_config()
        for profile in ("cheap", "main", "premium"):
            spec = select_model(profile, cfg)
            assert spec["provider"] == "openrouter"
            assert spec["model"]
            assert spec["profile"] == profile
            assert isinstance(spec["fallback_model"], list) and spec["fallback_model"]
            for fb in spec["fallback_model"]:
                assert set(fb) == {"provider", "model"}
                assert fb["provider"] and fb["model"]

    def test_unknown_profile_raises_key_error(self):
        cfg = load_model_config()
        with pytest.raises(KeyError):
            select_model("ultra-deluxe", cfg)

    def test_profile_missing_provider_raises_value_error(self):
        cfg = {"profiles": {"broken": {"model": "gpt-4o"}}}
        with pytest.raises(ValueError):
            select_model("broken", cfg)

    def test_profile_missing_model_raises_value_error(self):
        cfg = {"profiles": {"broken": {"provider": "openrouter"}}}
        with pytest.raises(ValueError):
            select_model("broken", cfg)

    def test_changing_yaml_changes_model_with_no_code_change(self, tmp_path):
        # Prompt 1.2 acceptance: "cambiar modelo = editar model_config.yaml, no código".
        path = _write_config(tmp_path)
        cfg = load_model_config(path)
        assert select_model("cheap", cfg)["model"] == "minimax/minimax-01"

        edited = REAL_CONFIG_YAML.replace("minimax/minimax-01", "some-other-org/some-other-model")
        with open(path, "w") as f:
            f.write(edited)
        cfg2 = load_model_config(path)
        assert select_model("cheap", cfg2)["model"] == "some-other-org/some-other-model"

    def test_fallback_chain_drops_malformed_entries(self):
        cfg = {
            "profiles": {
                "cheap": {
                    "provider": "openrouter",
                    "model": "minimax/minimax-01",
                    "fallback_chain": [
                        {"provider": "openrouter", "model": "openai/gpt-4o-mini"},
                        {"provider": "openrouter"},  # missing model -> dropped
                        {"model": "no-provider"},  # missing provider -> dropped
                        "not-a-dict",  # wrong type -> dropped
                    ],
                }
            }
        }
        spec = select_model("cheap", cfg)
        assert spec["fallback_model"] == [{"provider": "openrouter", "model": "openai/gpt-4o-mini"}]

    def test_profile_without_fallback_chain_returns_empty_list(self):
        cfg = {"profiles": {"cheap": {"provider": "openrouter", "model": "x"}}}
        assert select_model("cheap", cfg)["fallback_model"] == []


# ── Integration: select_model() feeds the EXISTING fallback mechanism ──────
#
# These tests deliberately do not reimplement retry/timeout/rate-limit
# handling. They build a real AIAgent using select_model()'s output, then
# drive the actual (already tested elsewhere) try_activate_fallback() to
# prove the router hands it correctly-shaped data.


def _make_agent_from_profile(profile: str):
    cfg = load_model_config()
    spec = select_model(profile, cfg)
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model=spec["model"],
            provider=spec["provider"],
            fallback_model=spec["fallback_model"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        return agent, spec


def _mock_fallback_client(model_id: str):
    mock = MagicMock()
    mock.base_url = "https://openrouter.ai/api/v1"
    mock.api_key = "fb-key"
    return mock


class TestRouterFeedsExistingFallbackMechanism:
    def test_router_output_populates_agent_fallback_chain(self):
        agent, spec = _make_agent_from_profile("main")
        assert agent.model == spec["model"]
        assert agent.provider == spec["provider"]
        assert agent._fallback_chain == spec["fallback_model"]
        assert agent._fallback_index == 0

    def test_timeout_reason_activates_fallback(self):
        """Prompt 1.2 acceptance: timeout -> fallback activates."""
        agent, spec = _make_agent_from_profile("main")
        fb_model = spec["fallback_model"][0]["model"]
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_fallback_client(fb_model), fb_model),
        ):
            activated = agent._try_activate_fallback(reason=FailoverReason.timeout)
        assert activated is True
        assert agent.model == fb_model
        assert agent._fallback_activated is True

    def test_rate_limit_429_reason_activates_fallback(self):
        """Prompt 1.2 acceptance: 429 -> fallback activates."""
        agent, spec = _make_agent_from_profile("cheap")
        fb_model = spec["fallback_model"][0]["model"]
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_fallback_client(fb_model), fb_model),
        ):
            activated = agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert activated is True
        assert agent.model == fb_model

    def test_fallback_exhausted_returns_false(self):
        # premium's chain has exactly one entry -- second call must fail.
        agent, spec = _make_agent_from_profile("premium")
        fb_model = spec["fallback_model"][0]["model"]
        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(_mock_fallback_client(fb_model), fb_model),
        ):
            assert agent._try_activate_fallback(reason=FailoverReason.timeout) is True
            assert agent._try_activate_fallback(reason=FailoverReason.timeout) is False

    def test_fallback_activation_is_logged(self, caplog):
        """Prompt 1.2 acceptance: every fallback activation is logged."""
        agent, spec = _make_agent_from_profile("main")
        fb_model = spec["fallback_model"][0]["model"]
        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_fallback_client(fb_model), fb_model),
            ),
            caplog.at_level(logging.INFO, logger="agent.chat_completion_helpers"),
        ):
            activated = agent._try_activate_fallback(reason=FailoverReason.rate_limit)
        assert activated is True
        # agent/chat_completion_helpers.py logs this exact line on every
        # successful activation, unconditionally -- provider/model/reason
        # all recoverable from it (or from old_model/old_provider in the
        # surrounding log context). We assert on the real message rather
        # than a message we invented, so this test breaks loudly if that
        # existing log line ever changes shape.
        assert any(
            "Fallback activated" in record.message and spec["model"] in record.message
            for record in caplog.records
        )
        assert any(fb_model in record.message for record in caplog.records)
