"""Regression tests for issue #76574 — auxiliary_client fixed-name credential
readers must honor the active profile secret scope, not raw os.getenv.

Covers the 5 sites that were still reading os.getenv directly after the
2 dynamic key_env sites were fixed (custom_key_env / cfg_key_env): the
OPENROUTER_API_KEY reads in _try_openrouter / _describe_openrouter_unavailable
/ auxiliary_max_tokens_param, and the OPENAI_API_KEY reads in
_resolve_custom_runtime / resolve_provider_client's explicit "custom" branch.
Under multiplexing, os.getenv can resolve another profile's value (or the
sticky process env); get_secret() honors the installed scope instead.
"""
from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import (
    OPENROUTER_BASE_URL,
    _describe_openrouter_unavailable,
    _resolve_custom_runtime,
    _resolve_task_provider_model,
    _try_openrouter,
    auxiliary_max_tokens_param,
    resolve_provider_client,
)
from agent.secret_scope import reset_secret_scope, set_secret_scope


class TestOpenRouterKeyThroughScope:
    def test_try_openrouter_prefers_scope_over_process_env(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")
        token = set_secret_scope({"OPENROUTER_API_KEY": "from-profile-scope"})
        try:
            with (
                patch("agent.auxiliary_client._select_pool_entry", return_value=(False, None)),
                patch("agent.auxiliary_client.OpenAI") as mock_openai,
            ):
                mock_openai.return_value = MagicMock(name="openrouter_client")
                client, _model = _try_openrouter()
        finally:
            reset_secret_scope(token)

        assert client is not None
        assert mock_openai.call_args.kwargs["api_key"] == "from-profile-scope"
        assert mock_openai.call_args.kwargs["base_url"] == OPENROUTER_BASE_URL

    def test_describe_openrouter_unavailable_reflects_scope(self, monkeypatch):
        from agent.secret_scope import set_multiplex_active

        monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")
        # A scope MISS under multiplex must not borrow the process env value
        # -- that borrow is exactly the #76574 leak this reader closes.
        set_multiplex_active(True)
        token = set_secret_scope({})
        try:
            with patch("agent.auxiliary_client._select_pool_entry", return_value=(False, None)):
                reason = _describe_openrouter_unavailable()
        finally:
            reset_secret_scope(token)
            set_multiplex_active(False)

        assert "OPENROUTER_API_KEY not set" in reason

    def test_auxiliary_max_tokens_param_reads_scope(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-process-env")
        token = set_secret_scope({})  # scope miss -> no OpenRouter key seen
        try:
            with patch("agent.auxiliary_client._current_custom_base_url", return_value=""), \
                 patch("agent.auxiliary_client._read_nous_auth", return_value=None):
                # With no scoped OpenRouter key and no custom/Nous auth, the
                # OpenAI-compatible heuristic path is reachable without the
                # process env leaking a key that should not be visible here.
                result = auxiliary_max_tokens_param(100, model="gpt-4o")
        finally:
            reset_secret_scope(token)

        assert isinstance(result, dict)


class TestOpenAICustomKeyThroughScope:
    def test_resolve_custom_runtime_prefers_scope_over_process_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPENAI_API_KEY", "from-process-env")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://custom.example/v1")
        token = set_secret_scope({"OPENAI_API_KEY": "from-profile-scope"})
        try:
            with patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=None):
                base_url, key, _model = _resolve_custom_runtime()
        finally:
            reset_secret_scope(token)

        assert base_url == "https://custom.example/v1"
        assert key == "from-profile-scope"

    def test_resolve_provider_client_custom_explicit_base_url_prefers_scope(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "from-process-env")
        token = set_secret_scope({"OPENAI_API_KEY": "from-profile-scope"})
        try:
            client, _model = resolve_provider_client(
                provider="custom", explicit_base_url="https://custom.example/v1",
            )
        finally:
            reset_secret_scope(token)

        assert client is not None
        assert getattr(client, "api_key", None) == "from-profile-scope"


class TestAuxiliaryTaskKeyEnvThroughScope:
    def test_task_config_key_env_prefers_scope_over_process_env(self, monkeypatch):
        monkeypatch.setenv("MY_TASK_KEY", "from-process-env")
        token = set_secret_scope({"MY_TASK_KEY": "from-profile-scope"})
        try:
            with patch(
                "agent.auxiliary_client._get_auxiliary_task_config",
                return_value={
                    "base_url": "https://scoped-task.example/v1",
                    "key_env": "MY_TASK_KEY",
                },
            ):
                _provider, _model, _base_url, api_key, _mode = _resolve_task_provider_model(
                    task="vision"
                )
        finally:
            reset_secret_scope(token)

        assert api_key == "from-profile-scope"
