"""Focused tests for Melious first-class provider wiring.

Melious ships as a plugin profile only — it has no ``HERMES_OVERLAYS`` entry
and no hand-written ``PROVIDER_REGISTRY`` row, so every assertion here is
really a check that one of the auto-wiring paths still covers a provider that
exists nowhere but ``plugins/model-providers/melious/``. If a refactor narrows
one of those paths, this file is what notices.

The exception is alias normalization, which is *not* auto-extended: without
explicit rows in ``providers.py::ALIASES`` and ``models.py::_PROVIDER_ALIASES``,
``melious-ai:glm-5.1`` silently parses as an OpenRouter model named
``melious-ai:glm-5.1`` — the "auth knows the provider but model parsing
doesn't" pitfall from the adding-providers guide.
"""

from __future__ import annotations

import sys
import types


if "dotenv" not in sys.modules:
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    sys.modules["dotenv"] = fake_dotenv


# Sibling provider keys that would otherwise let env auto-detect win a test
# that is meant to prove explicit config resolution.
_OTHER_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "UPSTAGE_API_KEY",
    "GMI_API_KEY",
)


class TestMeliousResolver:
    """``resolve_provider_full`` must recognise a plugin-only provider.

    Melious is absent from both models.dev's published catalog and
    ``HERMES_OVERLAYS``, so it resolves through the plugin-profile fallback in
    ``providers.py``. If that returns None, a saved ``provider: melious`` is
    discarded and resolution falls through to env auto-detect.
    """

    def test_resolve_provider_full_recognizes_melious(self):
        from hermes_cli.providers import resolve_provider_full

        pdef = resolve_provider_full("melious", {}, [])
        assert pdef is not None, (
            "resolve_provider_full('melious') returned None — config "
            "`provider: melious` would be discarded and auto-detect would win"
        )
        assert pdef.id == "melious"
        assert pdef.base_url == "https://api.melious.ai/v1"
        assert pdef.transport == "openai_chat"
        assert "MELIOUS_API_KEY" in pdef.api_key_env_vars

    def test_label_comes_from_the_profile(self):
        """No ``_LABEL_OVERRIDES`` row — the display name must survive anyway.

        Asserted because the overlay path lowercases an unknown id; only the
        plugin-profile path carries ``display_name`` through.
        """
        from hermes_cli.providers import get_label

        assert get_label("melious") == "Melious"


class TestMeliousAliasNormalization:
    """Every layer must collapse the alias to the same canonical id.

    Three separate tables are involved and only one of them auto-extends from
    the plugin registry, so this is checked layer by layer rather than through
    a single entry point.
    """

    def test_auth_layer(self):
        from hermes_cli.auth import resolve_provider

        assert resolve_provider("melious") == "melious"
        assert resolve_provider("melious-ai") == "melious"

    def test_providers_layer(self):
        from hermes_cli.providers import get_provider, normalize_provider

        assert normalize_provider("melious-ai") == "melious"
        # The id on the resolved def must be canonical too — a def carrying
        # id="melious-ai" would write an alias into config.yaml.
        pdef = get_provider("melious-ai")
        assert pdef is not None
        assert pdef.id == "melious"

    def test_models_layer(self):
        from hermes_cli.models import normalize_provider

        assert normalize_provider("melious-ai") == "melious"

    def test_provider_model_spec_parses(self):
        """``provider:model`` must not be mistaken for an OpenRouter model id."""
        from hermes_cli.models import parse_model_input

        assert parse_model_input("melious:glm-5.1", "openrouter") == (
            "melious",
            "glm-5.1",
        )
        assert parse_model_input("melious-ai:glm-5.1", "openrouter") == (
            "melious",
            "glm-5.1",
        )


class TestMeliousAuthRegistry:
    """``PROVIDER_REGISTRY`` is populated by the profile auto-extension.

    No hand-written row exists, so these assertions guard the derivation:
    the API key var must be separated from the base-URL override var, because
    the credential resolver sends the first ``api_key_env_vars`` entry as a
    Bearer token.
    """

    def test_registry_entry_derived_from_profile(self):
        from hermes_cli.auth import PROVIDER_REGISTRY

        assert "melious" in PROVIDER_REGISTRY
        pconfig = PROVIDER_REGISTRY["melious"]
        assert pconfig.id == "melious"
        assert pconfig.name == "Melious"
        assert pconfig.auth_type == "api_key"
        assert pconfig.inference_base_url == "https://api.melious.ai/v1"
        assert pconfig.api_key_env_vars == ("MELIOUS_API_KEY",)
        assert pconfig.base_url_env_var == "MELIOUS_BASE_URL"

    def test_credentials_resolve_from_env(self, monkeypatch):
        from hermes_cli.auth import resolve_api_key_provider_credentials

        for var in _OTHER_PROVIDER_KEYS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("MELIOUS_BASE_URL", raising=False)
        monkeypatch.setenv("MELIOUS_API_KEY", "sk-mel-test")

        creds = resolve_api_key_provider_credentials("melious")
        assert creds["api_key"] == "sk-mel-test"
        assert creds["base_url"] == "https://api.melious.ai/v1"

    def test_base_url_env_override_is_honored(self, monkeypatch):
        """``MELIOUS_BASE_URL`` has to reach credential resolution.

        The plugin-profile ``ProviderDef`` carries no ``base_url_env_var``, so
        this override survives only via the auth registry — worth pinning
        rather than assuming.
        """
        from hermes_cli.auth import resolve_api_key_provider_credentials

        monkeypatch.setenv("MELIOUS_API_KEY", "sk-mel-test")
        monkeypatch.setenv("MELIOUS_BASE_URL", "https://proxy.example.com/v1")

        creds = resolve_api_key_provider_credentials("melious")
        assert creds["base_url"] == "https://proxy.example.com/v1"


class TestMeliousEnvCatalog:
    """The dashboard/desktop Providers page lists only OPTIONAL_ENV_VARS keys
    whose category is "provider". Without these entries MELIOUS_API_KEY /
    MELIOUS_BASE_URL never reach the frontend and Melious stays invisible even
    though EnvPage.tsx has a matching PROVIDER_GROUPS prefix.

    These come from the static block in ``config_defaults.py`` rather than the
    dynamic profile injection in ``config.py``: that injection can run before
    provider discovery has populated the registry, and its idempotency flag
    makes the empty result permanent for the process.
    """

    def test_optional_env_vars_include_melious(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "MELIOUS_API_KEY" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["MELIOUS_API_KEY"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["MELIOUS_API_KEY"]["password"] is True
        assert OPTIONAL_ENV_VARS["MELIOUS_API_KEY"]["url"]

        assert "MELIOUS_BASE_URL" in OPTIONAL_ENV_VARS
        assert OPTIONAL_ENV_VARS["MELIOUS_BASE_URL"]["category"] == "provider"
        assert OPTIONAL_ENV_VARS["MELIOUS_BASE_URL"]["password"] is False


class TestMeliousPickerVisibility:
    def test_appears_in_canonical_providers(self):
        """Auto-extended from the plugin registry — no hand-written entry."""
        from hermes_cli.models import CANONICAL_PROVIDERS

        entry = next(
            (e for e in CANONICAL_PROVIDERS if e.slug == "melious"), None
        )
        assert entry is not None, "melious missing from the `hermes model` picker"
        assert entry.label == "Melious"
        assert entry.tui_desc


class TestMeliousConfigProviderWins:
    """An explicit config provider must beat env auto-detect."""

    def test_explicit_melious_beats_stray_sibling_key(self, monkeypatch):
        from hermes_cli.providers import resolve_provider_full

        monkeypatch.setenv("DEEPSEEK_API_KEY", "junk")
        monkeypatch.setenv("MELIOUS_API_KEY", "sk-mel-test")

        config_provider = "melious"  # from config model.provider
        active = ""
        if config_provider and config_provider != "auto":
            adef = resolve_provider_full(config_provider, {}, [])
            active = adef.id if adef is not None else ""

        assert active == "melious", (
            "explicit config provider should resolve to melious, not fall "
            "through to deepseek auto-detect"
        )
