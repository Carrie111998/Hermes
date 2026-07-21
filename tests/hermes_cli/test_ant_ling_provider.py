"""Tests for Ant Ling provider support.

Ant Ling is a Hermes-only OpenAI-compatible provider (not in models.dev):
  - base URL: https://api.ant-ling.com/v1
  - auth: API key (ANT_LING_API_KEY), Bearer
  - models: Ling-2.6-1T, Ling-2.6-flash, Ring-2.6-1T (reasoning), AntAngelMed
  - ``reasoning.effort`` is gated by an allow-list of reasoning-capable models
    (today: ``Ring-2.6-1T``). Future Ling releases (e.g. ``ling-3.0-flash``)
    that gain reasoning must be added to ``_REASONING_MODEL_MARKERS`` in the
    provider plugin — the list is intentionally exclusive so the field is never
    sent to a model that may reject or ignore it.
  - Model IDs are case-tolerant on the base name (only suffixes are
    sensitive), so ant-ling is deliberately NOT in
    _LOWERCASE_MODEL_PROVIDERS / _MATCHING_PREFIX_STRIP_PROVIDERS.

These tests mirror the shape of test_xiaomi_provider.py / test_gmi_provider.py.
"""

import pytest

from hermes_cli.auth import (
    PROVIDER_REGISTRY,
    resolve_provider,
    get_api_key_provider_status,
    resolve_api_key_provider_credentials,
)

ANT_LING_ENV_BLOCK = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY", "XAI_API_KEY", "KIMI_API_KEY",
    "MINIMAX_API_KEY", "KILOCODE_API_KEY", "HF_TOKEN", "GLM_API_KEY",
    "COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "MINIMAX_CN_API_KEY",
    "TOKENHUB_API_KEY", "ARCEEAI_API_KEY", "XIAOMI_API_KEY", "GMI_API_KEY",
    "STEPFUN_API_KEY", "NOUS_API_KEY",
)


# =============================================================================
# Provider Registry (auth.py)
# =============================================================================


class TestAntLingProviderRegistry:
    def test_registered(self):
        assert "ant-ling" in PROVIDER_REGISTRY

    def test_name(self):
        assert PROVIDER_REGISTRY["ant-ling"].name == "Ant Ling"

    def test_auth_type(self):
        assert PROVIDER_REGISTRY["ant-ling"].auth_type == "api_key"

    def test_inference_base_url(self):
        assert PROVIDER_REGISTRY["ant-ling"].inference_base_url == "https://api.ant-ling.com/v1"

    def test_api_key_env_vars(self):
        assert PROVIDER_REGISTRY["ant-ling"].api_key_env_vars == ("ANT_LING_API_KEY",)

    def test_base_url_env_var(self):
        assert PROVIDER_REGISTRY["ant-ling"].base_url_env_var == "ANT_LING_BASE_URL"


# =============================================================================
# Aliases
# =============================================================================


class TestAntLingAliases:
    @pytest.mark.parametrize("alias", ["ant-ling", "antling"])
    def test_alias_resolves(self, alias, monkeypatch):
        for key in ANT_LING_ENV_BLOCK:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("ANT_LING_API_KEY", "sk-test-key-12345678")
        assert resolve_provider(alias) == "ant-ling"

    def test_normalize_provider_models_py(self):
        from hermes_cli.models import normalize_provider
        assert normalize_provider("antling") == "ant-ling"

    def test_normalize_provider_providers_py(self):
        from hermes_cli.providers import normalize_provider
        assert normalize_provider("antling") == "ant-ling"


# =============================================================================
# Auto-detection
# =============================================================================


class TestAntLingAutoDetection:
    def test_auto_detect(self, monkeypatch):
        for var in ANT_LING_ENV_BLOCK:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("ANT_LING_API_KEY", "sk-ant-ling-test-12345678")
        assert resolve_provider("auto") == "ant-ling"


# =============================================================================
# Credentials
# =============================================================================


class TestAntLingCredentials:
    def test_status_configured(self, monkeypatch):
        monkeypatch.setenv("ANT_LING_API_KEY", "sk-test-12345678")
        assert get_api_key_provider_status("ant-ling")["configured"]

    def test_status_not_configured(self, monkeypatch):
        monkeypatch.delenv("ANT_LING_API_KEY", raising=False)
        assert not get_api_key_provider_status("ant-ling")["configured"]

    def test_resolve_credentials(self, monkeypatch):
        monkeypatch.setenv("ANT_LING_API_KEY", "sk-test-12345678")
        monkeypatch.delenv("ANT_LING_BASE_URL", raising=False)
        creds = resolve_api_key_provider_credentials("ant-ling")
        assert creds["api_key"] == "sk-test-12345678"
        assert creds["base_url"] == "https://api.ant-ling.com/v1"

    def test_custom_base_url_override(self, monkeypatch):
        monkeypatch.setenv("ANT_LING_API_KEY", "sk-test-12345678")
        monkeypatch.setenv("ANT_LING_BASE_URL", "https://custom.ant-ling.example/v1")
        creds = resolve_api_key_provider_credentials("ant-ling")
        assert creds["base_url"] == "https://custom.ant-ling.example/v1"


# =============================================================================
# Normalization — ant-ling is a direct, case-tolerant provider (NOT lowercased)
# =============================================================================


class TestAntLingNormalization:
    """Model IDs pass through unchanged (only suffixes are case-sensitive)."""

    def test_not_in_lowercase_set(self):
        from hermes_cli.model_normalize import _LOWERCASE_MODEL_PROVIDERS
        assert "ant-ling" not in _LOWERCASE_MODEL_PROVIDERS

    def test_not_in_prefix_strip_set(self):
        from hermes_cli.model_normalize import _MATCHING_PREFIX_STRIP_PROVIDERS
        assert "ant-ling" not in _MATCHING_PREFIX_STRIP_PROVIDERS

    @pytest.mark.parametrize("model", [
        "Ling-2.6-flash",
        "Ling-2.6-1T",
        "Ring-2.6-1T",
    ])
    def test_bare_name_unchanged(self, model):
        from hermes_cli.model_normalize import normalize_model_for_provider
        assert normalize_model_for_provider(model, "ant-ling") == model

    @pytest.mark.parametrize("empty_input", ["", None, "   "])
    def test_normalize_empty_and_none(self, empty_input):
        from hermes_cli.model_normalize import normalize_model_for_provider
        assert normalize_model_for_provider(empty_input, "ant-ling") == ""


# =============================================================================
# URL mapping (auto-derived from the plugin profile base_url)
# =============================================================================


class TestAntLingURLMapping:
    def test_url_to_provider(self):
        from agent.model_metadata import _URL_TO_PROVIDER
        assert _URL_TO_PROVIDER.get("api.ant-ling.com") == "ant-ling"

    def test_infer_from_url(self):
        from agent.model_metadata import _infer_provider_from_url
        assert _infer_provider_from_url("https://api.ant-ling.com/v1") == "ant-ling"


# =============================================================================
# providers.py overlay
# =============================================================================


class TestAntLingProvidersModule:
    def test_overlay_exists(self):
        from hermes_cli.providers import HERMES_OVERLAYS
        assert "ant-ling" in HERMES_OVERLAYS
        overlay = HERMES_OVERLAYS["ant-ling"]
        assert overlay.transport == "openai_chat"
        assert overlay.base_url_override == "https://api.ant-ling.com/v1"
        assert overlay.base_url_env_var == "ANT_LING_BASE_URL"
        assert "ANT_LING_API_KEY" in overlay.extra_env_vars
        assert not overlay.is_aggregator

    def test_label(self):
        from hermes_cli.providers import get_label
        assert get_label("ant-ling") == "Ant Ling"

    def test_get_provider(self):
        from hermes_cli.providers import get_provider
        pdef = get_provider("ant-ling")
        assert pdef is not None
        assert pdef.id == "ant-ling"
        assert pdef.transport == "openai_chat"
        assert pdef.base_url == "https://api.ant-ling.com/v1"
        assert pdef.source == "hermes"  # Hermes-only, not models.dev

    def test_api_mode_is_chat_completions(self):
        from hermes_cli.providers import determine_api_mode
        assert determine_api_mode("ant-ling") == "chat_completions"

    def test_resolve_alias_full(self):
        from hermes_cli.providers import resolve_provider_full
        pdef = resolve_provider_full("antling")
        assert pdef is not None
        assert pdef.id == "ant-ling"
        assert pdef.base_url == "https://api.ant-ling.com/v1"


# =============================================================================
# Plugin profile (providers/ discovery)
# =============================================================================


class TestAntLingProfile:
    def test_profile_registered(self):
        from providers import get_provider_profile
        p = get_provider_profile("ant-ling")
        assert p is not None
        assert p.name == "ant-ling"
        assert p.display_name == "Ant Ling"
        assert p.base_url == "https://api.ant-ling.com/v1"
        assert p.auth_type == "api_key"
        assert p.env_vars == ("ANT_LING_API_KEY",)
        assert "antling" in p.aliases

    def test_profile_in_canonical_providers(self):
        from hermes_cli.models import CANONICAL_PROVIDERS
        assert any(e.slug == "ant-ling" for e in CANONICAL_PROVIDERS)

    def test_fallback_models_non_empty(self):
        from providers import get_provider_profile
        p = get_provider_profile("ant-ling")
        assert len(p.fallback_models) >= 1
        assert "Ling-2.6-flash" in p.fallback_models


# =============================================================================
# Reasoning quirks — only Ring-2.6-1T, high/xhigh two-level collapse
# =============================================================================


class TestAntLingReasoning:
    """``reasoning.effort`` via extra_body for allow-listed reasoning models.

    Today only ``Ring-2.6-1T`` accepts the field; reasoning support is opt-in
    per model via ``_REASONING_MODEL_MARKERS``. Future Ling releases (e.g.
    ``ling-3.0-flash``) that gain reasoning must be added to the allow-list —
    the tests below pin both that current unlisted models drop the field, and
    the one-line extension path that adds a future model.
    """

    def _profile(self):
        from providers import get_provider_profile
        return get_provider_profile("ant-ling")

    @pytest.mark.parametrize("effort,expected", [
        ("xhigh", "xhigh"),
        ("max", "xhigh"),
        ("ultra", "xhigh"),
        ("high", "high"),
        ("medium", "high"),
        ("low", "high"),
        ("minimal", "high"),
    ])
    def test_ring_effort_collapse(self, effort, expected):
        extra_body, top_level = self._profile().build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="Ring-2.6-1T",
        )
        assert extra_body == {"reasoning": {"effort": expected}}
        assert top_level == {}

    def test_ring_disabled_omits_field(self):
        extra_body, _ = self._profile().build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            model="Ring-2.6-1T",
        )
        assert extra_body == {}

    def test_ring_no_preference_omits_field(self):
        extra_body, _ = self._profile().build_api_kwargs_extras(
            reasoning_config=None,
            model="Ring-2.6-1T",
        )
        assert extra_body == {}

    @pytest.mark.parametrize("model", ["Ling-2.6-flash", "Ling-2.6-1T"])
    def test_non_reasoning_never_sends_field(self, model):
        extra_body, _ = self._profile().build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model=model,
        )
        assert extra_body == {}

    @pytest.mark.parametrize("model", [
        "Ling-2.6-flash", "Ling-2.6-1T",
        "Ling-3.0-flash",          # future Ling not yet on the allow-list
        "ling-3.0-1t",
        "Ring-3.0-1T",             # future Ring not yet on the allow-list
    ])
    def test_unlisted_model_never_sends_field(self, model):
        """Allow-list is exclusive: anything not pinned drops the field.

        This is the deliberate cost of the allow-list: a future reasoning model
        like ``ling-3.0-flash`` will NOT receive the field until it is added to
        ``_REASONING_MODEL_MARKERS``. That keeps the field off undocumented
        models in the meantime — see ``test_adding_marker_enables_field`` for
        the one-line extension path.
        """
        extra_body, _ = self._profile().build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model=model,
        )
        assert extra_body == {}

    def test_spelling_independent_match(self):
        """Allow-list must match across ``.``/``_`` separator spellings."""
        for variant in (
            "Ring-2.6-1T",            # canonical
            "ring-2.6-1t",            # lowercased
            "ring-2-6-1t",            # hyphen-joined
            "ring_2_6_1t",            # underscore-joined
            "ant-ling/Ring-2.6-1T",   # vendor-prefixed
        ):
            extra_body, _ = self._profile().build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "xhigh"},
                model=variant,
            )
            assert extra_body == {"reasoning": {"effort": "xhigh"}}, \
                f"allow-list failed for {variant!r}"

    @pytest.mark.parametrize("model", [None, "", "   "])
    def test_unresolved_model_omits_field(self, model):
        """No model resolved → never emit the field (relay may route anywhere)."""
        extra_body, _ = self._profile().build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model=model,
        )
        assert extra_body == {}

    def test_adding_marker_enables_field(self):
        """Document the one-line extension path for future reasoning models.

        When ``ling-3.0-flash`` gains reasoning, adding its normalised marker to
        ``_REASONING_MODEL_MARKERS`` is all that's needed — this test simulates
        that and asserts the field then flows through. The marker is restored
        afterwards so it can never leak into the live allow-list.

        The plugin is loaded under a synthetic module path via ``importlib`` at
        discovery time, so we reach the module through ``sys.modules`` rather
        than a normal import (``plugins.model_providers`` is not a real package).
        """
        import sys

        self._profile()  # trigger provider discovery → populates sys.modules
        mod = sys.modules["plugins.model_providers.ant_ling"]

        marker_norm = "ling-3-0-flash"
        original = mod._REASONING_MODEL_MARKERS
        mod._REASONING_MODEL_MARKERS = original + (marker_norm,)
        try:
            extra_body, _ = self._profile().build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "xhigh"},
                model="Ling-3.0-flash",
            )
            assert extra_body == {"reasoning": {"effort": "xhigh"}}
        finally:
            mod._REASONING_MODEL_MARKERS = original


# =============================================================================
# Pricing (usage_pricing.py) — promotional RMB→USD snapshot
# =============================================================================


class TestAntLingPricing:
    @pytest.mark.parametrize("model", ["Ling-2.6-flash", "Ling-2.6-1T", "Ring-2.6-1T"])
    def test_entry_exists(self, model):
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING
        assert ("ant-ling", model) in _OFFICIAL_DOCS_PRICING

    def test_flash_cheaper_than_1t(self):
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING
        flash = _OFFICIAL_DOCS_PRICING[("ant-ling", "Ling-2.6-flash")]
        one_t = _OFFICIAL_DOCS_PRICING[("ant-ling", "Ling-2.6-1T")]
        assert flash.input_cost_per_million < one_t.input_cost_per_million
        assert flash.output_cost_per_million < one_t.output_cost_per_million

    def test_ring_same_as_ling_1t(self):
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING
        ring = _OFFICIAL_DOCS_PRICING[("ant-ling", "Ring-2.6-1T")]
        ling = _OFFICIAL_DOCS_PRICING[("ant-ling", "Ling-2.6-1T")]
        assert ring.input_cost_per_million == ling.input_cost_per_million
        assert ring.output_cost_per_million == ling.output_cost_per_million

    def test_source_and_url(self):
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING
        e = _OFFICIAL_DOCS_PRICING[("ant-ling", "Ling-2.6-flash")]
        assert e.source == "official_docs_snapshot"
        assert "ant-ling.com" in (e.source_url or "")


# =============================================================================
# Doctor & dump
# =============================================================================


class TestAntLingDoctor:
    def test_provider_env_hints(self):
        from hermes_cli.doctor import _PROVIDER_ENV_HINTS
        assert "ANT_LING_API_KEY" in _PROVIDER_ENV_HINTS


class TestAntLingDump:
    def test_dump_listed(self):
        import inspect
        from hermes_cli import dump
        # api_keys list is a local inside run_dump; assert via source.
        src = inspect.getsource(dump.run_dump)
        assert "ANT_LING_API_KEY" in src


# =============================================================================
# Agent init sanity
# =============================================================================


class TestAntLingAgentInit:
    def test_no_syntax_errors(self):
        import importlib
        importlib.import_module("run_agent")