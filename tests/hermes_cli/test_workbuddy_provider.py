"""Tests for the WorkBuddy provider (www.workbuddy.ai/v2).

WorkBuddy is Tencent's consumer AI assistant exposed over an
OpenAI-compatible endpoint. Two properties make it more than "another
base URL + API key", and both are asserted here:

1. It is stream-only. A non-streaming chat request returns HTTP 400
   ``{"code": 11101, "msg": "Non-stream chat request is currently not
   supported"}`` — the same envelope as Tencent Copilot. Auxiliary tasks
   (title generation, compression, vision) use the non-streaming path, so
   without handling they fail on every call.
2. It exposes tier aliases (``default-model``, ``fast-model``, ...) that
   resolve server-side, plus named models advertised by ``/v3/config``.
"""

import pytest

from hermes_cli.auth import PROVIDER_REGISTRY, resolve_provider
from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_MODELS


# Other provider env vars to clear during auto-detection tests.
_OTHER_PROVIDER_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY",
    "XAI_API_KEY", "KIMI_API_KEY", "KIMI_CN_API_KEY",
    "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "AI_GATEWAY_API_KEY",
    "KILOCODE_API_KEY", "HF_TOKEN", "GLM_API_KEY", "ZAI_API_KEY",
    "XIAOMI_API_KEY", "OPENROUTER_API_KEY", "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN", "GITHUB_TOKEN", "ARCEEAI_API_KEY",
    "TOKENHUB_API_KEY", "TOKENPLAN_API_KEY",
)


# =============================================================================
# Provider registry
# =============================================================================


class TestWorkBuddyProviderRegistry:
    """Verify workbuddy is registered as a first-class provider."""

    def test_registered(self):
        assert "workbuddy" in PROVIDER_REGISTRY

    def test_inference_base_url(self):
        assert (
            PROVIDER_REGISTRY["workbuddy"].inference_base_url
            == "https://www.workbuddy.ai/v2"
        )

    def test_api_key_env_var(self):
        assert PROVIDER_REGISTRY["workbuddy"].api_key_env_vars == (
            "WORKBUDDY_ACCESS_TOKEN",
        )

    def test_base_url_env_var(self):
        assert PROVIDER_REGISTRY["workbuddy"].base_url_env_var == "WORKBUDDY_BASE_URL"

    def test_auth_type_is_api_key(self):
        # The token is obtained out-of-band (browser OAuth), but every
        # inference request authenticates with a plain Bearer token, so
        # Hermes' standard api_key path applies.
        assert PROVIDER_REGISTRY["workbuddy"].auth_type == "api_key"


# =============================================================================
# Aliases — these gate `provider:model` parsing and /model
# =============================================================================


@pytest.mark.parametrize("alias", ["workbuddy", "workbuddy-ai", "wb"])
def test_alias_resolves(alias, monkeypatch):
    """`--provider <alias>` and `provider:model` must reach workbuddy."""
    for key in _OTHER_PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WORKBUDDY_ACCESS_TOKEN", "wb-test-token")
    assert resolve_provider(alias) == "workbuddy"


@pytest.mark.parametrize("alias", ["workbuddy", "workbuddy-ai", "wb"])
def test_normalize_provider_models_py(alias):
    from hermes_cli.models import normalize_provider

    assert normalize_provider(alias) == "workbuddy"


@pytest.mark.parametrize("alias", ["workbuddy", "workbuddy-ai", "wb"])
def test_normalize_provider_providers_py(alias):
    from hermes_cli.providers import normalize_provider

    assert normalize_provider(alias) == "workbuddy"


# =============================================================================
# Model catalog
# =============================================================================


class TestWorkBuddyModelCatalog:
    def test_catalog_present(self):
        assert "workbuddy" in _PROVIDER_MODELS

    def test_tier_aliases_listed(self):
        models = _PROVIDER_MODELS["workbuddy"]
        for slot in (
            "default-model",
            "fast-model",
            "balanced-model",
            "primary-model",
            "deep-model",
        ):
            assert slot in models

    def test_no_empty_entries(self):
        assert all(m and m.strip() for m in _PROVIDER_MODELS["workbuddy"])

    def test_offered_in_model_picker(self):
        slugs = {p.slug for p in CANONICAL_PROVIDERS}
        assert "workbuddy" in slugs


# =============================================================================
# Stream-only handling — the bug this provider would otherwise hit
# =============================================================================


class TestWorkBuddyRequiresStream:
    """Aux calls to WorkBuddy must be sent with stream=True.

    Reproduces the real failure: without this, title generation returned
    HTTP 400 code 11101 because the auxiliary path never sets ``stream``.
    """

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://www.workbuddy.ai/v2",
            "https://www.workbuddy.ai",
            "https://api.workbuddy.ai/v2",
        ],
    )
    def test_workbuddy_endpoints_require_stream(self, base_url):
        from agent.auxiliary_client import _provider_requires_stream

        assert _provider_requires_stream("workbuddy", base_url) is True

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.openai.com/v1",
            "https://openrouter.ai/api/v1",
            "https://api.anthropic.com",
        ],
    )
    def test_other_endpoints_unaffected(self, base_url):
        from agent.auxiliary_client import _provider_requires_stream

        assert _provider_requires_stream("custom", base_url) is False

    def test_no_base_url_is_not_stream_only(self):
        from agent.auxiliary_client import _provider_requires_stream

        assert _provider_requires_stream("workbuddy", None) is False
        assert _provider_requires_stream("workbuddy", "") is False

    def test_lookalike_host_does_not_match(self):
        """Guard the substring-match bug class (see base_url_host_matches)."""
        from agent.auxiliary_client import _provider_requires_stream

        assert (
            _provider_requires_stream("custom", "https://evil.com/workbuddy.ai/v1")
            is False
        )
        assert _provider_requires_stream("custom", "https://workbuddy.ai.evil/v1") is False


# =============================================================================
# Auxiliary model routing
# =============================================================================


def test_aux_model_registered():
    """Without this, aux tasks leak to the session default provider."""
    from agent.auxiliary_client import _API_KEY_PROVIDER_AUX_MODELS

    assert _API_KEY_PROVIDER_AUX_MODELS.get("workbuddy")


def test_hostname_maps_to_workbuddy():
    """model_metadata reverse-maps base URL host -> provider id.

    Without this entry, a WorkBuddy endpoint reached via a custom base URL
    is treated as an unknown endpoint and loses context-window resolution.
    """
    from agent.model_metadata import _URL_TO_PROVIDER

    assert _URL_TO_PROVIDER.get("www.workbuddy.ai") == "workbuddy"
    assert _URL_TO_PROVIDER.get("workbuddy.ai") == "workbuddy"
