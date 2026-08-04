"""Tests for GatewayRunner._format_session_info — session config surfacing."""

import pytest
from unittest.mock import patch

from gateway.run import GatewayRunner


@pytest.fixture()
def runner():
    """Create a bare GatewayRunner without __init__."""
    return GatewayRunner.__new__(GatewayRunner)


def _patch_info(tmp_path, config_yaml, model, runtime):
    """Return a context-manager stack that patches _format_session_info deps."""
    cfg_path = tmp_path / "config.yaml"
    if config_yaml is not None:
        cfg_path.write_text(config_yaml)
    return (
        patch("gateway.run._hermes_home", tmp_path),
        patch("gateway.run._resolve_gateway_model", return_value=model),
        patch("gateway.run._resolve_runtime_agent_kwargs", return_value=runtime),
    )


class TestFormatSessionInfo:

    def test_includes_model_name(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: anthropic/claude-opus-4.6\n  provider: openrouter\n",
                                  "anthropic/claude-opus-4.6",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "claude-opus-4.6" in info

    def test_includes_provider(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
                                  "test-model",
                                  {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "openrouter" in info

    def test_config_context_length(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 32768\n",
                                  "test-model",
                                  {"provider": "custom", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "32K" in info
        assert "config" in info

    def test_default_fallback_hint(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: unknown-model-xyz\n",
                                  "unknown-model-xyz",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "256K" in info
        assert "model.context_length" in info

    def test_local_endpoint_shown(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(
            tmp_path,
            "model:\n  default: qwen3:8b\n  provider: custom\n  base_url: http://localhost:11434/v1\n  context_length: 8192\n",
            "qwen3:8b",
            {"provider": "custom", "base_url": "http://localhost:11434/v1", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "localhost:11434" in info
        assert "8K" in info

    def test_cloud_endpoint_hidden(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  provider: openrouter\n",
                                  "test-model",
                                  {"provider": "openrouter", "base_url": "https://openrouter.ai/api/v1", "api_key": "k"})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "Endpoint" not in info

    def test_million_context_format(self, runner, tmp_path):
        p1, p2, p3 = _patch_info(tmp_path, "model:\n  default: test-model\n  context_length: 1000000\n",
                                  "test-model",
                                  {"provider": "", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "1.0M" in info

    def test_custom_context_is_scoped_to_active_runtime_route(self, runner, tmp_path):
        config = """
model:
  default: shared-model
  provider: custom
custom_providers:
  - name: large-route
    base_url: https://example.com/v1//
    models:
      shared-model:
        context_length: 1048576
"""
        p1, p2, p3 = _patch_info(
            tmp_path,
            config,
            "shared-model",
            {
                "provider": "custom",
                "base_url": "https://example.com/v1",
                "api_key": "k",
            },
        )

        with p1, p2, p3:
            info = runner._format_session_info()

        assert "1.0M" not in info
        assert "(config)" not in info

    def test_global_context_is_scoped_to_active_runtime_route(self, runner, tmp_path):
        config = """
model:
  default: shared-model
  provider: custom
  base_url: https://large.example/v1
  context_length: 1048576
"""
        p1, p2, p3 = _patch_info(
            tmp_path,
            config,
            "shared-model",
            {
                "provider": "custom",
                "base_url": "https://small.example/v1",
                "api_key": "k",
            },
        )

        with p1, p2, p3:
            info = runner._format_session_info()

        assert "1.0M" not in info
        assert "(config)" not in info

    def test_missing_config(self, runner, tmp_path):
        """No config.yaml should not crash."""
        p1, p2, p3 = _patch_info(tmp_path, None,  # don't create config
                                  "anthropic/claude-sonnet-4.6",
                                  {"provider": "openrouter", "base_url": "", "api_key": ""})
        with p1, p2, p3:
            info = runner._format_session_info()
        assert "Model" in info
        assert "Context" in info

    def test_runtime_resolution_failure_doesnt_crash(self, runner, tmp_path):
        """If runtime resolution raises, should still produce output."""
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("model:\n  default: test-model\n  context_length: 4096\n")
        with patch("gateway.run._hermes_home", tmp_path), \
             patch("gateway.run._resolve_gateway_model", return_value="test-model"), \
             patch("gateway.run._resolve_runtime_agent_kwargs", side_effect=RuntimeError("no creds")):
            info = runner._format_session_info()
        assert "4K" in info
        assert "config" in info

    def test_named_custom_provider_keeps_context_pin_without_model_base_url(
        self, runner, tmp_path
    ):
        """Session-reset banner must honor model.context_length for named custom providers.

        Repro: /status shows 262144 from config while the reset banner said
        ``131K tokens (detected)`` because empty model.base_url + runtime URL
        falsely cleared the pin and fell through to the Qwen family default.
        """
        model = "custom-local-agentw/Qwen-AgentWorld-35B-A3B-Q5_K_XL"
        config_yaml = (
            "model:\n"
            f"  default: {model}\n"
            "  provider: custom-local-agentw\n"
            "  context_length: 262144\n"
            "custom_providers:\n"
            "  - name: custom-local-agentw\n"
            "    base_url: http://127.0.0.1:8080/v1\n"
            "    models: {}\n"
        )
        p1, p2, p3 = _patch_info(
            tmp_path,
            config_yaml,
            model,
            {
                "provider": "custom-local-agentw",
                "base_url": "http://127.0.0.1:8080/v1",
                "api_key": "",
            },
        )
        with p1, p2, p3, patch(
            "hermes_cli.config.get_compatible_custom_providers",
            return_value=[
                {
                    "name": "custom-local-agentw",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "models": {},
                }
            ],
        ), patch(
            "agent.model_metadata.get_model_context_length",
            side_effect=lambda *args, **kwargs: (
                kwargs.get("config_context_length")
                if kwargs.get("config_context_length")
                else 131072
            ),
        ):
            info = runner._format_session_info()
        assert "262K" in info
        assert "config" in info
        assert "131K" not in info


class TestResetNoticeSessionInfo:
    """#59003: the auto-reset banner must report the serving profile's config,
    using the profile identity frozen when that gateway process was built."""

    _RUNTIME = {"provider": "", "base_url": "", "api_key": ""}

    def _source(self, profile="planner"):
        from gateway.config import Platform
        from gateway.session import SessionSource
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id="123", user_id="u1",
            profile=profile,
        )

    def _homes(self, tmp_path):
        base = tmp_path / "base"
        profile = tmp_path / "profiles" / "planner"
        profile.mkdir(parents=True)
        base.mkdir()
        base.joinpath("config.yaml").write_text(
            "model:\n  default: base-model\n  provider: custom\n  context_length: 1000\n")
        profile.joinpath("config.yaml").write_text(
            "model:\n  default: profile-model\n  provider: anthropic\n  context_length: 2000\n")
        return base, profile

    def test_named_process_uses_its_frozen_profile_config(self, runner, tmp_path):
        from types import SimpleNamespace
        from tests.gateway._profile_authority import install_frozen_profile_authority

        base, profile = self._homes(tmp_path)
        install_frozen_profile_authority(runner, profile, profile="planner")
        runner.config = SimpleNamespace(multiplex_profiles=False)
        with patch("gateway.run._hermes_home", base), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source())
        assert "profile-model" in info
        assert "anthropic" in info
        assert "base-model" not in info

    def test_single_profile_uses_base_config(self, runner, tmp_path):
        from types import SimpleNamespace
        from tests.gateway._profile_authority import install_frozen_profile_authority

        base, _profile = self._homes(tmp_path)
        install_frozen_profile_authority(runner, base)
        runner.config = SimpleNamespace(multiplex_profiles=False)
        with patch("gateway.run._hermes_home", base), \
             patch("gateway.run._resolve_runtime_agent_kwargs", return_value=self._RUNTIME):
            info = runner._reset_notice_session_info(self._source(profile=None))
        assert "base-model" in info
        assert "profile-model" not in info
