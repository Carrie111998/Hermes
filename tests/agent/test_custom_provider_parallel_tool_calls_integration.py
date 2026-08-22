from types import SimpleNamespace

import pytest
import yaml

from agent.agent_init import _merge_custom_provider_extra_body
from agent.transports.chat_completions import ChatCompletionsTransport
from hermes_cli.config import get_compatible_custom_providers
from hermes_cli.runtime_provider import resolve_runtime_provider
from providers import get_provider_profile


@pytest.mark.parametrize("configured", [True, False, None], ids=["true", "false", "unset"])
def test_temp_home_custom_provider_parallel_tool_calls_reaches_wire(
    tmp_path, monkeypatch, configured
):
    hermes_home = tmp_path / f"hermes-{configured}"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    provider_config = {
        "api": "https://api.vendor.example.com/v1",
        "api_key": "test-key",
        "default_model": "vendor-model",
        "extra_body": {"include_reasoning": True},
    }
    if configured is not None:
        provider_config["parallel_tool_calls"] = configured
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"provider": "vendor", "default": "vendor-model"},
                "providers": {"vendor": provider_config},
            }
        ),
        encoding="utf-8",
    )

    custom_providers = get_compatible_custom_providers()
    resolved = resolve_runtime_provider(requested="vendor")
    agent = SimpleNamespace(
        provider=resolved["provider"],
        model=resolved.get("model", "vendor-model"),
        base_url=resolved["base_url"],
        request_overrides={},
    )
    _merge_custom_provider_extra_body(agent, custom_providers)

    kwargs = ChatCompletionsTransport().build_kwargs(
        model=agent.model,
        messages=[{"role": "user", "content": "Hi"}],
        tools=[{"type": "function", "function": {"name": "test", "parameters": {}}}],
        provider_profile=get_provider_profile("custom"),
        request_overrides=agent.request_overrides,
    )

    assert kwargs["extra_body"]["include_reasoning"] is True
    if configured is None:
        assert "parallel_tool_calls" not in resolved.get("request_overrides", {})
        assert "parallel_tool_calls" not in agent.request_overrides
        assert "parallel_tool_calls" not in kwargs
    else:
        assert resolved["request_overrides"]["parallel_tool_calls"] is configured
        assert agent.request_overrides["parallel_tool_calls"] is configured
        assert kwargs["parallel_tool_calls"] is configured
