"""Attribution default_headers applied per provider via base-URL detection.

Mirrors the OpenRouter pattern for the Vercel AI Gateway so that
referrerUrl / appName / User-Agent flow into gateway analytics.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def test_named_loopback_ollama_does_not_apply_configured_provider_headers():
    """Local named Ollama must not reattach headers dropped by its resolver."""
    agent = AIAgent.__new__(AIAgent)
    agent._client_kwargs = {}
    agent.provider = "custom"
    agent.requested_provider = "ollama"
    agent.api_mode = "chat_completions"
    agent._apply_user_default_headers = lambda: None

    with patch("hermes_cli.config.load_config", return_value={
        "providers": {
            "ollama": {
                "base_url": "http://127.0.0.1:11434/v1",
                "extra_headers": {"Authorization": "header-credential-sentinel"},
            }
        }
    }):
        agent._apply_client_headers_for_base_url("http://127.0.0.1:11434/v1")

    assert "default_headers" not in agent._client_kwargs


def test_named_remote_ollama_keeps_configured_provider_headers():
    """The named local boundary does not suppress remote Ollama headers."""
    agent = AIAgent.__new__(AIAgent)
    agent._client_kwargs = {}
    agent.provider = "custom"
    agent.requested_provider = "ollama"
    agent.api_mode = "chat_completions"
    agent._apply_user_default_headers = lambda: None

    with patch("hermes_cli.config.load_config", return_value={
        "providers": {
            "ollama": {
                "base_url": "https://ollama.remote.example/v1",
                "extra_headers": {"Authorization": "remote-header-sentinel"},
            }
        }
    }):
        agent._apply_client_headers_for_base_url("https://ollama.remote.example/v1")

    assert agent._client_kwargs["default_headers"] == {
        "Authorization": "remote-header-sentinel"
    }


def _write_ollama_header_config(tmp_path, *, base_url):
    (tmp_path / "config.yaml").write_text(
        f"""\
model:
  default_headers:
    Authorization: model-default-header-sentinel
  extra_headers:
    X-Model-Token: model-extra-header-sentinel
providers:
  ollama:
    base_url: {base_url}
    extra_headers:
      X-Provider-Token: provider-header-sentinel
""",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("provider", "requested_provider"),
    [("custom", "ollama"), ("ollama", None)],
)
def test_full_constructor_and_rebuild_drop_all_local_ollama_headers(
    tmp_path, monkeypatch, provider, requested_provider
):
    """Both client creation paths isolate model and provider headers locally."""
    base_url = "http://127.0.0.1:11434/v1"
    _write_ollama_header_config(tmp_path, base_url=base_url)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))

    agent = AIAgent(
        api_key="no-key-required",
        base_url=base_url,
        provider=provider,
        requested_provider=requested_provider,
        api_mode="chat_completions",
        model="local-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent._client_kwargs.get("default_headers") is None
    agent._apply_client_headers_for_base_url(base_url)
    assert agent._client_kwargs.get("default_headers") is None


def test_full_constructor_and_rebuild_keep_remote_ollama_headers(tmp_path, monkeypatch):
    """Remote Ollama retains configured credentials and header behavior."""
    base_url = "https://ollama.remote.example/v1"
    _write_ollama_header_config(tmp_path, base_url=base_url)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("run_agent.OpenAI", MagicMock(return_value=MagicMock()))

    agent = AIAgent(
        api_key="remote-credential-sentinel",
        base_url=base_url,
        provider="custom",
        requested_provider="ollama",
        api_mode="chat_completions",
        model="remote-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    expected_headers = {
        "Authorization": "model-default-header-sentinel",
        "X-Model-Token": "model-extra-header-sentinel",
        "X-Provider-Token": "provider-header-sentinel",
    }
    assert agent.api_key == "remote-credential-sentinel"
    assert agent._client_kwargs["default_headers"] == expected_headers
    agent._apply_client_headers_for_base_url(base_url)
    assert agent._client_kwargs["default_headers"] == expected_headers


@patch("run_agent.OpenAI")
def test_openrouter_base_url_applies_or_headers(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://openrouter.ai/api/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-Title"] == "Hermes Agent"


@patch("run_agent.OpenAI")
def test_ai_gateway_base_url_applies_attribution_headers(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://ai-gateway.vercel.sh/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-Title"] == "Hermes Agent"
    assert headers["User-Agent"].startswith("HermesAgent/")


@patch("run_agent.OpenAI")
def test_routermint_base_url_applies_user_agent_header(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://api.routermint.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://api.routermint.com/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["User-Agent"].startswith("HermesAgent/")


@patch("run_agent.OpenAI")
def test_nvidia_cloud_base_url_applies_billing_origin_header(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        model="nvidia/test-model",
        provider="nvidia",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    assert agent._client_kwargs["default_headers"]["X-BILLING-INVOKE-ORIGIN"] == "HermesAgent"

    agent._apply_client_headers_for_base_url("https://integrate.api.nvidia.com/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["X-BILLING-INVOKE-ORIGIN"] == "HermesAgent"


@patch("run_agent.OpenAI")
def test_fireworks_applies_attribution_via_profile_fallback(mock_openai):
    """Fireworks has no host-specific branch — its attribution headers come
    from the profile.default_headers fallback, the path a model switch
    re-runs."""
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://api.fireworks.ai/inference/v1",
        model="accounts/fireworks/models/kimi-k2p6",
        provider="fireworks",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://api.fireworks.ai/inference/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-Title"] == "Hermes Agent"
    assert headers["User-Agent"].startswith("HermesAgent/")


@patch("run_agent.OpenAI")
def test_opencode_go_applies_attribution_via_profile_fallback(mock_openai):
    """OpenCode (Zen/Go) attributes traffic by header like OpenRouter does.
    Without profile.default_headers the relay only sees the OpenAI SDK's
    generic User-Agent and Hermes Agent traffic shows up unattributed."""
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1",
        model="glm-5",
        provider="opencode-go",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://opencode.ai/zen/go/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-Title"] == "Hermes Agent"
    assert headers["User-Agent"].startswith("HermesAgent/")


@patch("run_agent.OpenAI")
def test_opencode_zen_applies_attribution_via_profile_fallback(mock_openai):
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://opencode.ai/zen/v1",
        model="claude-sonnet-4-5",
        provider="opencode-zen",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    agent._apply_client_headers_for_base_url("https://opencode.ai/zen/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-Title"] == "Hermes Agent"
    assert headers["User-Agent"].startswith("HermesAgent/")


@patch("run_agent.OpenAI")
def test_routed_client_preserves_openai_sdk_custom_headers(mock_openai):
    mock_openai.return_value = MagicMock()
    routed_client = SimpleNamespace(
        api_key="test-key",
        base_url="https://integrate.api.nvidia.com/v1",
        _custom_headers={"X-BILLING-INVOKE-ORIGIN": "HermesAgent"},
    )

    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(
        routed_client,
        "nvidia/test-model",
    )):
        agent = AIAgent(
            provider="nvidia",
            model="nvidia/test-model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    headers = agent._client_kwargs["default_headers"]
    assert headers["X-BILLING-INVOKE-ORIGIN"] == "HermesAgent"












@patch("run_agent.OpenAI")
def test_openrouter_headers_include_response_cache_when_enabled(mock_openai):
    """When openrouter.response_cache is True, the cache header is injected."""
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    with patch("hermes_cli.config.load_config", return_value={
        "openrouter": {"response_cache": True, "response_cache_ttl": 600},
    }), patch("hermes_cli.config.load_config_readonly", return_value={
        "openrouter": {"response_cache": True, "response_cache_ttl": 600},
    }):
        agent._apply_client_headers_for_base_url("https://openrouter.ai/api/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert headers["X-OpenRouter-Cache"] == "true"
    assert headers["X-OpenRouter-Cache-TTL"] == "600"


# ---------------------------------------------------------------------------
# model.default_headers — user-configured overrides (#40033)
# ---------------------------------------------------------------------------


@patch("run_agent.OpenAI")
def test_user_default_headers_override_sdk_user_agent(mock_openai):
    """``model.default_headers`` lets a custom endpoint swap the OpenAI SDK
    User-Agent that some gateways/WAFs reject (the #40033 reproduction)."""
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="http://localhost:8080/v1",
        model="my-custom-model",
        provider="custom",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    with patch("hermes_cli.config.load_config", return_value={
        "model": {"default_headers": {"User-Agent": "curl/8.7.1", "X-Extra": "1"}},
    }), patch("hermes_cli.config.load_config_readonly", return_value={
        "model": {"default_headers": {"User-Agent": "curl/8.7.1", "X-Extra": "1"}},
    }):
        agent._apply_client_headers_for_base_url("http://localhost:8080/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["User-Agent"] == "curl/8.7.1"
    assert headers["X-Extra"] == "1"








@patch("run_agent.OpenAI")
def test_openrouter_headers_no_cache_when_disabled(mock_openai):
    """When openrouter.response_cache is False, no cache headers are sent."""
    mock_openai.return_value = MagicMock()
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    with patch("hermes_cli.config.load_config", return_value={
        "openrouter": {"response_cache": False},
    }), patch("hermes_cli.config.load_config_readonly", return_value={
        "openrouter": {"response_cache": False},
    }):
        agent._apply_client_headers_for_base_url("https://openrouter.ai/api/v1")

    headers = agent._client_kwargs["default_headers"]
    assert headers["HTTP-Referer"] == "https://hermes-agent.nousresearch.com"
    assert "X-OpenRouter-Cache" not in headers
    assert "X-OpenRouter-Cache-TTL" not in headers
