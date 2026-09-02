"""Focused tests for Synthetic (synthetic.new) first-class provider wiring."""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

if "dotenv" not in sys.modules:
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = fake_dotenv

from hermes_cli.auth import resolve_provider
from hermes_cli.config import load_config
from hermes_cli.models import (
    CANONICAL_PROVIDERS,
    _PROVIDER_LABELS,
    _PROVIDER_MODELS,
    normalize_provider,
    provider_model_ids,
)
from agent.auxiliary_client import resolve_provider_client


@pytest.fixture(autouse=True)
def _clear_provider_env(monkeypatch):
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "SYNTHETIC_API_KEY",
        "SYNTHETIC_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


class TestSyntheticAliases:
    @pytest.mark.parametrize("alias", ["synthetic", "synthetic-new", "syntheticnew"])
    def test_alias_resolves(self, alias, monkeypatch):
        monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-test-key")
        assert resolve_provider(alias) == "synthetic"

    def test_models_normalize_provider(self):
        assert normalize_provider("synthetic-new") == "synthetic"

    def test_providers_normalize_provider(self):
        from hermes_cli.providers import normalize_provider as normalize_provider_in_providers

        assert normalize_provider_in_providers("synthetic-new") == "synthetic"


class TestSyntheticConfigRegistry:
    def test_optional_env_vars_include_synthetic(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "SYNTHETIC_API_KEY" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["SYNTHETIC_API_KEY"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["SYNTHETIC_API_KEY"]["password"] is True
        assert OPTIONAL_ENV_VARS["SYNTHETIC_API_KEY"]["url"] == "https://synthetic.new/"

        assert "SYNTHETIC_BASE_URL" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["SYNTHETIC_BASE_URL"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["SYNTHETIC_BASE_URL"]["password"] is False


class TestSyntheticProfile:
    def test_profile_registers_with_expected_endpoint(self):
        from providers import get_provider_profile

        profile = get_provider_profile("synthetic")
        assert profile is not None
        assert profile.auth_type == "api_key"
        assert profile.base_url == "https://api.synthetic.new/openai/v1"
        assert "SYNTHETIC_API_KEY" in profile.env_vars
        assert "SYNTHETIC_BASE_URL" in profile.env_vars

    def test_profile_hostname_derives_from_base_url(self):
        from providers import get_provider_profile

        profile = get_provider_profile("synthetic")
        assert profile.get_hostname() == "api.synthetic.new"

    def test_profile_aux_model_uses_syn_alias(self):
        """syn: aliases never 404 on upstream model rotation — the aux default
        must be an alias, not a pinned hf: catalog ID."""
        from providers import get_provider_profile

        profile = get_provider_profile("synthetic")
        assert profile.default_aux_model.startswith("syn:")


class TestSyntheticModelCatalog:
    def test_canonical_provider_entry(self):
        slugs = [p.slug for p in CANONICAL_PROVIDERS]
        assert "synthetic" in slugs

    def test_curated_catalog_is_syn_aliases_only(self):
        """The curated list must contain ONLY syn: aliases — pinned hf: IDs
        are deliberately excluded so upstream model rotation can never 404
        them; the live /openai/v1/models catalog supplies them at picker
        time instead."""
        curated = _PROVIDER_MODELS["synthetic"]
        assert any(m.startswith("syn:") for m in curated)
        for m in curated:
            assert m.startswith("syn:"), m

    def test_provider_profile_fallback_models_are_syn_aliases_only(self):
        from providers import get_provider_profile

        profile = get_provider_profile("synthetic")
        for m in profile.fallback_models:
            assert m.startswith("syn:"), m

    def test_provider_model_ids_merges_live_api(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {
                "provider": provider_id,
                "api_key": "synthetic-live-key",
                "base_url": "https://api.synthetic.new/openai/v1",
                "source": "SYNTHETIC_API_KEY",
            },
        )
        monkeypatch.setattr(
            "hermes_cli.models.fetch_api_models",
            lambda api_key, base_url: ["syn:large:text", "hf:zai-org/GLM-5.2"],
        )

        ids = provider_model_ids("synthetic")
        assert ids[0] == "syn:large:text"  # curated-first merge
        assert len(ids) >= len(_PROVIDER_MODELS["synthetic"])

    def test_provider_model_ids_falls_back_to_curated(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {
                "provider": provider_id,
                "api_key": "",
                "base_url": "",
                "source": "",
            },
        )

        assert provider_model_ids("synthetic") == list(_PROVIDER_MODELS["synthetic"])


class TestSyntheticProvidersModule:
    def test_overlay_exists(self):
        from hermes_cli.providers import HERMES_OVERLAYS

        assert "synthetic" in HERMES_OVERLAYS
        overlay = HERMES_OVERLAYS["synthetic"]
        assert overlay.transport == "openai_chat"
        assert overlay.extra_env_vars == ("SYNTHETIC_API_KEY",)
        assert overlay.base_url_override == "https://api.synthetic.new/openai/v1"
        assert overlay.base_url_env_var == "SYNTHETIC_BASE_URL"
        assert not overlay.is_aggregator

    def test_provider_label(self):
        assert _PROVIDER_LABELS["synthetic"] == "Synthetic"


class TestSyntheticDoctor:
    def test_provider_env_hints_include_synthetic(self):
        from hermes_cli.doctor import _PROVIDER_ENV_HINTS

        assert "SYNTHETIC_API_KEY" in _PROVIDER_ENV_HINTS


class TestSyntheticAuxiliary:
    def test_resolve_provider_client_uses_synthetic_aux_default(self, monkeypatch):
        monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-test-key")

        with patch("agent.auxiliary_client.OpenAI") as mock_openai:
            mock_openai.return_value = object()
            client, model = resolve_provider_client("synthetic")

        assert client is not None
        assert model == "syn:small:text"
        assert mock_openai.call_args.kwargs["api_key"] == "synthetic-test-key"
        assert mock_openai.call_args.kwargs["base_url"] == "https://api.synthetic.new/openai/v1"


class TestSyntheticMainFlow:
    def test_chat_parser_accepts_synthetic_provider(self, monkeypatch):
        recorded: dict[str, str] = {}

        monkeypatch.setattr("hermes_cli.config.get_container_exec_info", lambda: None)
        monkeypatch.setattr(
            "hermes_cli.main.cmd_chat",
            lambda args: recorded.setdefault("provider", args.provider),
        )
        monkeypatch.setattr(sys, "argv", ["hermes", "chat", "--provider", "synthetic"])

        from hermes_cli.main import main

        main()

        assert recorded["provider"] == "synthetic"

    def test_select_provider_and_model_routes_synthetic_to_generic_flow(self, monkeypatch):
        recorded: dict[str, str] = {}

        monkeypatch.setattr("hermes_cli.auth.resolve_provider", lambda *args, **kwargs: None)

        def fake_prompt_provider_choice(choices, default=0):
            return next(i for i, label in enumerate(choices) if label.startswith("Synthetic"))

        def fake_model_flow_api_key_provider(config, provider_id, current_model=""):
            recorded["provider_id"] = provider_id

        monkeypatch.setattr("hermes_cli.main._prompt_provider_choice", fake_prompt_provider_choice)
        monkeypatch.setattr("hermes_cli.main._model_flow_api_key_provider", fake_model_flow_api_key_provider)

        from hermes_cli.main import select_provider_and_model

        select_provider_and_model()

        assert recorded["provider_id"] == "synthetic"

    def test_model_flow_api_key_provider_persists_synthetic_selection(self, monkeypatch):
        monkeypatch.setenv("SYNTHETIC_API_KEY", "synthetic-test-key")

        with patch(
            "hermes_cli.models.fetch_api_models",
            return_value=["syn:large:text", "syn:small:text"],
        ), patch(
            "hermes_cli.auth._prompt_model_selection",
            return_value="syn:large:text",
        ), patch(
            "hermes_cli.auth.deactivate_provider",
        ), patch(
            "builtins.input",
            return_value="",
        ):
            from hermes_cli.main import _model_flow_api_key_provider

            _model_flow_api_key_provider(load_config(), "synthetic", "old-model")

        import yaml
        from hermes_constants import get_hermes_home

        config = yaml.safe_load((get_hermes_home() / "config.yaml").read_text()) or {}
        model_cfg = config.get("model")
        assert isinstance(model_cfg, dict)
        assert model_cfg["provider"] == "synthetic"
        assert model_cfg["default"] == "syn:large:text"
        assert model_cfg["base_url"] == "https://api.synthetic.new/openai/v1"