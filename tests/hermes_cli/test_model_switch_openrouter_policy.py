"""OpenRouter policy-authority regression tests for model switching."""

from unittest.mock import patch

from hermes_cli.model_switch import list_authenticated_providers, switch_model
import hermes_cli.models as models_mod


_REJECTED = {
    "accepted": False,
    "persist": False,
    "recognized": False,
    "message": "blocked by OpenRouter policy",
}


def test_saved_openrouter_model_cannot_override_policy_rejection():
    """A local provider declaration must not bypass /models/user authority."""
    with (
        patch("hermes_cli.model_switch.resolve_alias", return_value=None),
        patch("hermes_cli.model_switch.list_provider_models", return_value=[]),
        patch(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            return_value={
                "api_key": "test-key",
                "base_url": "https://openrouter.ai/api/v1",
                "api_mode": "chat_completions",
            },
        ),
        patch("hermes_cli.models.validate_requested_model", return_value=_REJECTED),
        patch("hermes_cli.model_switch.get_model_info", return_value=None),
        patch("hermes_cli.model_switch.get_model_capabilities", return_value=None),
        patch("hermes_cli.models.detect_provider_for_model", return_value=None),
    ):
        result = switch_model(
            raw_input="blocked/model",
            current_provider="openrouter",
            current_model="allowed/model",
            explicit_provider="openrouter",
            user_providers={
                "openrouter": {
                    "enabled": True,
                    "models": ["blocked/model"],
                }
            },
        )

    assert result.success is False
    assert result.error_message == "blocked by OpenRouter policy"


class _CatalogResponse:
    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._data


def _list_openrouter(
    monkeypatch,
    tmp_path,
    *,
    response: bytes | Exception,
    user_providers=None,
    configured_api_key: str | None = None,
):
    monkeypatch.setattr("hermes_cli.auth._load_auth_store", lambda: {})
    if configured_api_key is None:
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    else:
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config_readonly",
            lambda: {
                "model": {
                    "provider": "openrouter",
                    "api_key": configured_api_key,
                    "base_url": "https://openrouter.ai/api/v1",
                }
            },
        )
    monkeypatch.setattr(models_mod, "_provider_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr("agent.models_dev.PROVIDER_TO_MODELS_DEV", {"openrouter": "openrouter"})
    monkeypatch.setattr(
        "agent.models_dev.fetch_models_dev",
        lambda: {"openrouter": {"name": "OpenRouter", "env": ["OPENROUTER_API_KEY"]}},
    )
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})
    monkeypatch.setattr(models_mod, "CANONICAL_PROVIDERS", [])
    monkeypatch.setattr(
        "hermes_cli.model_catalog.get_curated_openrouter_models",
        lambda: [("curated/model", "recommended"), ("verified/model", "")],
    )
    models_mod.clear_provider_models_cache("openrouter")

    def _open(*_args, **_kwargs):
        if isinstance(response, Exception):
            raise response
        return _CatalogResponse(response)

    with patch("hermes_cli.models._urlopen_model_catalog_request", side_effect=_open):
        return list_authenticated_providers(user_providers=user_providers or {})


def test_unavailable_openrouter_catalog_yields_no_curated_picker_models(monkeypatch, tmp_path):
    providers = _list_openrouter(
        monkeypatch,
        tmp_path,
        response=OSError("offline"),
    )

    openrouter = next(provider for provider in providers if provider["slug"] == "openrouter")
    assert openrouter["models"] == []
    assert openrouter["total_models"] == 0


def test_openrouter_declarations_only_reorder_verified_catalog(monkeypatch, tmp_path):
    providers = _list_openrouter(
        monkeypatch,
        tmp_path,
        user_providers={
            "openrouter": {
                "enabled": True,
                "models": ["blocked/model", "verified/model"],
            }
        },
        response=(
            b'{"data":['
            b'{"id":"curated/model","supported_parameters":["tools"]},'
            b'{"id":"verified/model","supported_parameters":["tools"]}'
            b']}'
        ),
    )

    openrouter = next(provider for provider in providers if provider["slug"] == "openrouter")
    assert openrouter["models"] == ["verified/model", "curated/model"]
    assert "blocked/model" not in openrouter["models"]


def test_configured_only_openrouter_credential_reaches_policy_picker(monkeypatch, tmp_path):
    providers = _list_openrouter(
        monkeypatch,
        tmp_path,
        configured_api_key="configured-only-key",
        response=(
            b'{"data":['
            b'{"id":"verified/model","supported_parameters":["tools"]}'
            b']}'
        ),
    )

    openrouter = next(provider for provider in providers if provider["slug"] == "openrouter")
    assert openrouter["models"] == ["verified/model"]
