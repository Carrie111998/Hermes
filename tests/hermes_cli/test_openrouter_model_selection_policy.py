"""OpenRouter family policy must reject `/model` before state changes."""

from types import SimpleNamespace

from hermes_cli.model_switch import switch_model
from hermes_cli.providers import ProviderDef


_OPENROUTER = ProviderDef(
    id="openrouter",
    name="OpenRouter",
    transport="openai_chat",
    api_key_env_vars=("OPENROUTER_API_KEY",),
    base_url="https://openrouter.ai/api/v1",
    is_aggregator=True,
    auth_type="api_key",
    source="test",
)

_OPENAI_API_VIA_OPENROUTER = ProviderDef(
    id="openai-api",
    name="OpenAI-compatible OpenRouter",
    transport="openai_chat",
    api_key_env_vars=("OPENROUTER_API_KEY",),
    base_url="https://openrouter.ai/api/v1",
    is_aggregator=False,
    auth_type="api_key",
    source="test",
)


def _provider(provider_id):
    return _OPENAI_API_VIA_OPENROUTER if provider_id == "openai-api" else _OPENROUTER


def _switch(model, monkeypatch, provider="openrouter"):
    monkeypatch.setattr("hermes_cli.model_switch.resolve_provider_full", lambda *args: _provider(provider))
    monkeypatch.setattr("hermes_cli.model_switch.resolve_alias", lambda *args: None)
    return switch_model(
        raw_input=model,
        current_provider="custom",
        current_model="google/gemini-2.5-pro",
        current_base_url="http://localhost:11434/v1",
        explicit_provider=provider,
        is_global=True,
    )


def test_openrouter_rejects_openai_family_before_runtime_resolution(monkeypatch):
    def _unexpected(*args, **kwargs):
        raise AssertionError("rejected model must not resolve credentials")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", _unexpected)

    result = _switch("openai/gpt-5.6", monkeypatch)

    assert not result.success
    assert result.target_provider == "openrouter"
    assert "forbidden model family" in result.error_message


def test_openrouter_rejects_anthropic_claude_and_openai_api_url_form(monkeypatch):
    result = _switch("anthropic/claude-sonnet-4.6", monkeypatch, provider="openai-api")

    assert not result.success
    assert result.target_provider == "openai-api"
    assert "forbidden model family" in result.error_message


def test_openrouter_allows_non_openai_non_anthropic_model(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": "test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *args, **kwargs: {"accepted": True, "persist": True, "message": ""},
    )
    monkeypatch.setattr("hermes_cli.model_switch.normalize_model_for_provider", lambda model, provider: model)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *args: None)
    monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *args: None)

    result = _switch("google/gemini-2.5-pro", monkeypatch)

    assert result.success
    assert result.new_model == "google/gemini-2.5-pro"


def test_cli_model_rejection_does_not_mutate_or_persist(monkeypatch):
    import cli as cli_mod

    state = SimpleNamespace(
        provider="openrouter",
        model="google/gemini-2.5-pro",
        base_url="https://openrouter.ai/api/v1",
        api_key="test-key",
        agent=None,
    )
    before = (state.provider, state.model, state.base_url, state.api_key)
    output = []
    persisted = []

    monkeypatch.setattr("hermes_cli.model_switch.resolve_provider_full", lambda *args: _OPENROUTER)
    monkeypatch.setattr("hermes_cli.model_switch.resolve_alias", lambda *args: None)
    monkeypatch.setattr(cli_mod, "_cprint", lambda text, *args, **kwargs: output.append(str(text)))
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *args, **kwargs: persisted.append(args))

    cli_mod.HermesCLI._handle_model_switch(
        state, "/model openai/gpt-5.6 --provider openrouter"
    )

    assert (state.provider, state.model, state.base_url, state.api_key) == before
    assert persisted == []
    assert any("forbidden model family" in line for line in output)
