from types import SimpleNamespace

from providers.base import ProviderProfile

from hermes_cli import providers as provider_catalog
from hermes_cli import runtime_provider


class _DynamicProfile(ProviderProfile):
    def resolve_api_mode(self, model, base_url=None):
        return (
            "codex_responses"
            if str(model or "").startswith("gpt-")
            else "chat_completions"
        )


def test_determine_api_mode_delegates_to_profile_with_target_model(monkeypatch):
    profile = _DynamicProfile(
        name="dynamic-test",
        api_mode="chat_completions",
        base_url="https://relay.test/v1",
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: profile if name == "dynamic-test" else None,
    )

    assert provider_catalog.determine_api_mode(
        "dynamic-test", "https://relay.test/v1", "gpt-7"
    ) == "codex_responses"
    assert provider_catalog.determine_api_mode(
        "dynamic-test", "https://relay.test/v1", "kimi-k4"
    ) == "chat_completions"


def test_runtime_fallback_passes_model_to_profile_hook(monkeypatch):
    profile = _DynamicProfile(
        name="dynamic-test",
        api_mode="chat_completions",
        base_url="https://relay.test/v1",
    )
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: profile if name == "dynamic-test" else None,
    )

    assert runtime_provider._fallback_api_mode(
        "dynamic-test", "https://relay.test/v1", "gpt-7"
    ) == "codex_responses"
    assert runtime_provider._fallback_api_mode(
        "dynamic-test", "https://relay.test/v1", "kimi-k4"
    ) == "chat_completions"


def test_host_mandated_mode_still_wins_over_profile_hook(monkeypatch):
    profile = _DynamicProfile(name="dynamic-test", api_mode="chat_completions")
    monkeypatch.setattr("providers.get_provider_profile", lambda name: profile)

    assert provider_catalog.determine_api_mode(
        "dynamic-test", "https://api.anthropic.com", "gpt-7"
    ) == "anthropic_messages"


def test_static_profile_does_not_override_existing_catalog_transport(monkeypatch):
    profile = ProviderProfile(name="static-test", api_mode="codex_responses")
    monkeypatch.setattr("providers.get_provider_profile", lambda name: profile)
    monkeypatch.setattr(
        provider_catalog,
        "get_provider",
        lambda name: SimpleNamespace(transport="openai_chat"),
    )

    assert provider_catalog.determine_api_mode(
        "static-test", "https://proxy.test/v1", "gpt-7"
    ) == "chat_completions"


def test_raising_profile_hook_falls_back_to_existing_transport(monkeypatch):
    class RaisingProfile(ProviderProfile):
        def resolve_api_mode(self, model, base_url=None):
            raise RuntimeError("plugin boom")

    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: RaisingProfile(name="raising-test"),
    )
    monkeypatch.setattr(
        provider_catalog,
        "get_provider",
        lambda name: SimpleNamespace(transport="openai_chat"),
    )

    assert provider_catalog.determine_api_mode(
        "raising-test", "https://relay.test/v1", "gpt-7"
    ) == "chat_completions"


def test_invalid_profile_hook_result_falls_back_to_existing_transport(monkeypatch):
    class InvalidProfile(ProviderProfile):
        def resolve_api_mode(self, model, base_url=None):
            return "shell_exec"

    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: InvalidProfile(name="invalid-test"),
    )
    monkeypatch.setattr(
        provider_catalog,
        "get_provider",
        lambda name: SimpleNamespace(transport="anthropic_messages"),
    )

    assert provider_catalog.determine_api_mode(
        "invalid-test", "https://relay.test/v1", "gpt-7"
    ) == "anthropic_messages"


def test_unhashable_profile_hook_result_falls_back_to_existing_transport(monkeypatch):
    class InvalidProfile(ProviderProfile):
        def resolve_api_mode(self, model, base_url=None):  # type: ignore[override]
            return []

    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: InvalidProfile(name="invalid-unhashable-test"),
    )
    monkeypatch.setattr(
        provider_catalog,
        "get_provider",
        lambda name: SimpleNamespace(transport="openai_chat"),
    )

    assert provider_catalog.determine_api_mode(
        "invalid-unhashable-test", "https://relay.test/v1", "gpt-7"
    ) == "chat_completions"


def test_core_alias_resolves_same_dynamic_profile_hook(monkeypatch):
    profile = _DynamicProfile(name="canonical-test", api_mode="chat_completions")
    monkeypatch.setitem(provider_catalog.ALIASES, "short-test", "canonical-test")
    monkeypatch.setattr(
        "providers.get_provider_profile",
        lambda name: profile if name == "canonical-test" else None,
    )

    assert provider_catalog.determine_api_mode(
        "short-test", "https://relay.test/v1", "gpt-7"
    ) == "codex_responses"