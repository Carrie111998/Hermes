"""Regression contracts for Azure Foundry picker discovery with Entra ID.

These tests are hermetic: they exercise the picker boundary without minting a
real token or reading a real Azure endpoint.
"""

from __future__ import annotations


def test_provider_model_ids_uses_runtime_entra_token_provider(monkeypatch):
    """The picker must use the same callable Entra credential as inference."""
    from hermes_cli import azure_detect, runtime_provider
    from hermes_cli.models import provider_model_ids

    calls = []
    token_provider = lambda: "test-bearer-token"

    monkeypatch.setattr(
        runtime_provider,
        "_resolve_azure_foundry_runtime",
        lambda **kwargs: {
            "provider": "azure-foundry",
            "base_url": "https://test.services.ai.azure.com/models/v1",
            "api_key": token_provider,
            "auth_mode": "entra_id",
        },
    )

    def probe(base_url, api_key, *, token_provider=None):
        calls.append((base_url, api_key, token_provider))
        return True, ["deployment-terra", "deployment-luna"]

    monkeypatch.setattr(azure_detect, "_probe_openai_models", probe)

    assert provider_model_ids("azure-foundry", force_refresh=True) == [
        "deployment-terra",
        "deployment-luna",
    ]
    assert calls == [
        (
            "https://test.services.ai.azure.com/models/v1",
            "",
            token_provider,
        )
    ]


def test_entra_only_azure_foundry_is_present_in_picker_inventory(monkeypatch):
    """A valid keyless Foundry route must not be hidden before discovery."""
    from hermes_cli import auth, models
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        auth,
        "_get_azure_foundry_auth_status",
        lambda: {
            "logged_in": True,
            "auth_mode": "entra_id",
            "azure_identity_installed": True,
        },
    )
    monkeypatch.setattr(
        models,
        "cached_provider_model_ids",
        lambda provider, **kwargs: ["deployment-terra"]
        if provider == "azure-foundry"
        else [],
    )

    rows = list_authenticated_providers(
        current_provider="azure-foundry",
        current_model="deployment-terra",
        custom_providers=[],
        for_picker=True,
    )

    azure = next(row for row in rows if row["slug"] == "azure-foundry")
    assert azure["models"] == ["deployment-terra"]
    assert azure["is_current"] is True


def test_picker_keeps_configured_foundry_alias_when_live_probe_is_empty(monkeypatch):
    """A transient empty probe must not make the active deployment unselectable."""
    from hermes_cli import azure_detect, runtime_provider
    from hermes_cli.models import provider_model_ids

    monkeypatch.setattr(
        runtime_provider,
        "_resolve_azure_foundry_runtime",
        lambda **kwargs: {
            "provider": "azure-foundry",
            "base_url": "https://test.services.ai.azure.com/models/v1",
            "api_key": lambda: "test-bearer-token",
            "auth_mode": "entra_id",
        },
    )
    monkeypatch.setattr(
        azure_detect,
        "_probe_openai_models",
        lambda *args, **kwargs: (True, []),
    )
    monkeypatch.setattr(
        "hermes_cli.models._get_model_config_dict",
        lambda: {
            "provider": "azure-foundry",
            "default": "deployment-terra",
        },
    )

    assert provider_model_ids("azure-foundry", force_refresh=True) == [
        "deployment-terra"
    ]


def test_picker_keeps_configured_foundry_alias_when_live_probe_raises(monkeypatch):
    """A failed discovery request must be no more destructive than an empty one."""
    from hermes_cli import azure_detect, runtime_provider
    from hermes_cli.models import provider_model_ids

    monkeypatch.setattr(
        runtime_provider,
        "_resolve_azure_foundry_runtime",
        lambda **kwargs: {
            "provider": "azure-foundry",
            "base_url": "https://test.services.ai.azure.com/models/v1",
            "api_key": lambda: "test-bearer-token",
            "auth_mode": "entra_id",
        },
    )
    monkeypatch.setattr(
        azure_detect,
        "_probe_openai_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    monkeypatch.setattr(
        "hermes_cli.models._get_model_config_dict",
        lambda: {
            "provider": "azure-foundry",
            "default": "deployment-terra",
        },
    )

    assert provider_model_ids("azure-foundry", force_refresh=True) == [
        "deployment-terra"
    ]
