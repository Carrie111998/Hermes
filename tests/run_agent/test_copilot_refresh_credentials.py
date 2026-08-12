"""Regression coverage for Copilot 401 credential refresh routing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


def _write_model_config(model_cfg: dict) -> None:
    home = Path(os.environ["HERMES_HOME"])
    config_path = home / "config.yaml"
    lines = ["model:"]
    for key, value in model_cfg.items():
        lines.append(f"  {key}: {value}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from hermes_cli import config as config_mod

    for cache_name in (
        "_LOAD_CONFIG_CACHE",
        "_RAW_CONFIG_CACHE",
        "_LAST_EXPANDED_CONFIG_BY_PATH",
    ):
        cache = getattr(config_mod, cache_name, None)
        if cache is not None and hasattr(cache, "clear"):
            cache.clear()


def _make_copilot_agent():
    from run_agent import AIAgent

    agent: Any = AIAgent.__new__(AIAgent)
    agent.provider = "copilot"
    agent.api_mode = "codex_responses"
    agent.api_key = "stale-copilot-api-token"
    agent.base_url = "https://api.business.githubcopilot.com"
    agent._client_kwargs = {
        "api_key": "stale-copilot-api-token",
        "base_url": "https://api.business.githubcopilot.com",
    }
    agent._replace_primary_openai_client = MagicMock(return_value=True)
    agent._apply_client_headers_for_base_url = MagicMock()
    return agent


def test_configured_copilot_refresh_does_not_borrow_generic_github_token(monkeypatch):
    """A pinned Copilot route must fail closed during 401 refresh as well."""
    from hermes_cli import copilot_auth

    _write_model_config(
        {
            "provider": "copilot",
            "default": "gpt-5.5",
            "api_mode": "codex_responses",
            "base_url": "https://api.business.githubcopilot.com",
        }
    )
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "gho_generic_github_token")
    monkeypatch.setattr(copilot_auth, "_try_gh_cli_token", lambda: "gho_from_gh_cli")
    monkeypatch.setattr(
        copilot_auth,
        "get_copilot_api_token",
        lambda raw_token: (f"api-for-{raw_token}", "https://api.githubcopilot.com"),
    )
    agent = _make_copilot_agent()

    assert agent._try_refresh_copilot_client_credentials() is False
    assert agent.api_key == "stale-copilot-api-token"
    agent._replace_primary_openai_client.assert_not_called()


def test_configured_copilot_refresh_uses_exchanged_copilot_token(monkeypatch):
    """401 refresh keeps using the configured Copilot token family."""
    from hermes_cli import copilot_auth

    _write_model_config(
        {
            "provider": "copilot",
            "default": "gpt-5.5",
            "api_mode": "codex_responses",
            "base_url": "https://api.business.githubcopilot.com",
        }
    )
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "gho_work_token")
    monkeypatch.setenv("GITHUB_TOKEN", "gho_generic_github_token")
    monkeypatch.setattr(copilot_auth, "_try_gh_cli_token", lambda: "")
    monkeypatch.setattr(
        copilot_auth,
        "get_copilot_api_token",
        lambda raw_token: (f"api-for-{raw_token}", "https://api.githubcopilot.com"),
    )
    agent = _make_copilot_agent()

    assert agent._try_refresh_copilot_client_credentials() is True
    assert agent.api_key == "api-for-gho_work_token"
    assert agent._client_kwargs["api_key"] == "api-for-gho_work_token"
    assert agent._client_kwargs["base_url"] == "https://api.business.githubcopilot.com"
    agent._replace_primary_openai_client.assert_called_once_with(
        reason="copilot_credential_refresh"
    )