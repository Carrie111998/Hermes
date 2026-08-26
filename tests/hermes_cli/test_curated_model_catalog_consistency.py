"""Curated model lists stay aligned across pickers, plugins, and docs fixtures."""

from __future__ import annotations

import pytest

from agent.model_metadata import DEFAULT_CONTEXT_LENGTHS
from hermes_cli.models import (
    OPENROUTER_MODELS,
    VERCEL_AI_GATEWAY_MODELS,
    _PROVIDER_MODELS,
)
from providers import get_provider_profile, list_providers

# Profiles whose ``fallback_models`` are intentionally not picker rows
# (e.g. legacy portal aliases). Everything else with both a fallback chain
# and a curated ``_PROVIDER_MODELS`` list must keep fallback ⊆ curated.
_FALLBACK_CURATED_EXCEPTIONS = frozenset({"nous"})


def _openrouter_ids() -> list[str]:
    return [mid for mid, _ in OPENROUTER_MODELS]


def _gateway_ids() -> list[str]:
    return [mid for mid, _ in VERCEL_AI_GATEWAY_MODELS]


def _providers_for_fallback_curated_guard() -> list[str]:
    """Every registered profile with fallbacks + a curated picker list."""
    return sorted(
        p.name
        for p in list_providers()
        if p.fallback_models
        and p.name in _PROVIDER_MODELS
        and p.name not in _FALLBACK_CURATED_EXCEPTIONS
    )


@pytest.mark.parametrize("provider", _providers_for_fallback_curated_guard())
def test_provider_fallback_models_in_curated_catalog(provider: str):
    """Plugin fallback chains must only reference picker-visible model ids."""
    profile = get_provider_profile(provider)
    assert profile is not None
    curated = set(_PROVIDER_MODELS[provider])
    missing = [mid for mid in profile.fallback_models if mid not in curated]
    assert not missing, (
        f"{provider} fallback_models not in _PROVIDER_MODELS[{provider!r}]: {missing}"
    )


def test_zai_flash_models_in_curated_catalog():
    for mid in ("glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash", "glm-4-9b"):
        assert mid in _PROVIDER_MODELS["zai"]


def test_nvidia_expanded_hosted_models_in_curated_catalog():
    expected = (
        "nvidia/nemotron-3-nano-30b-a3b",
        "nvidia/nvidia-nemotron-nano-9b-v2",
        "nvidia/nemotron-nano-12b-v2-vl",
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "google/gemma-4-31b-it",
        "meta/llama-3.3-70b-instruct",
        "stepfun-ai/step-3.7-flash",
        "poolside/laguna-xs-2.1",
        "nvidia/llama-3.1-nemotron-70b-instruct",
        "nvidia/llama-3.3-70b-instruct",
    )
    curated = _PROVIDER_MODELS["nvidia"]
    missing = [mid for mid in expected if mid not in curated]
    assert not missing, f"nvidia curated list missing: {missing}"


def test_huggingface_glm_4_7_flash_in_curated_catalog():
    assert "zai-org/GLM-4.7-Flash" in _PROVIDER_MODELS["huggingface"]
    assert "Qwen/Qwen3.5-72B-Instruct" in _PROVIDER_MODELS["huggingface"]


def test_huggingface_curated_models_have_context_length_keys():
    """Picker Hub IDs must have a case-insensitive DEFAULT_CONTEXT_LENGTHS entry.

    Resolution lowercases the selected model before substring-matching catalog
    keys, so mixed Hub casing in the picker still needs a matching key under
    ``.lower()`` (see ``zai-org/glm-4.7-flash`` vs curated ``GLM-4.7-Flash``).
    """
    keys_lower = {key.lower() for key in DEFAULT_CONTEXT_LENGTHS}
    missing = [
        mid
        for mid in _PROVIDER_MODELS["huggingface"]
        if mid.lower() not in keys_lower
    ]
    assert not missing, (
        "huggingface curated models missing case-insensitive "
        f"DEFAULT_CONTEXT_LENGTHS keys: {missing}"
    )


def test_tencent_tokenhub_hy3_in_curated_catalog():
    assert "hy3" in _PROVIDER_MODELS["tencent-tokenhub"]


def test_vercel_ai_gateway_free_models_in_curated_catalog():
    free_models = (
        "meta/llama-3.3-70b",
        "meta/llama-4-scout",
        "meta/llama-4-maverick",
        "poolside/laguna-s-2.1-free",
    )
    gateway = _gateway_ids()
    missing = [mid for mid in free_models if mid not in gateway]
    assert not missing, f"VERCEL_AI_GATEWAY_MODELS missing: {missing}"


def test_openrouter_router_models_in_curated_catalog():
    for mid in ("openrouter/pareto-code", "openrouter/auto-beta", "openrouter/free"):
        assert mid in _openrouter_ids()
