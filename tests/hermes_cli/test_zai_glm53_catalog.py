"""Regression coverage for direct Z.AI GLM-5.3 discovery and context."""

from __future__ import annotations

from unittest.mock import patch

from agent.model_metadata import get_model_context_length
from hermes_cli.models import _PROVIDER_MODELS, validate_requested_model


def test_glm_53_uses_one_million_token_context() -> None:
    with (
        patch("agent.model_metadata.get_cached_context_length", return_value=None),
        patch("agent.model_metadata.fetch_endpoint_model_metadata", return_value={}),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
        patch("agent.models_dev.lookup_models_dev_context", return_value=None),
    ):
        assert get_model_context_length("glm-5.3", provider="zai") == 1_048_576


def test_zai_picker_leads_with_glm_53() -> None:
    assert _PROVIDER_MODELS["zai"][0] == "glm-5.3"


def test_zai_validation_uses_curated_glm_53_when_models_endpoint_lags() -> None:
    with patch("hermes_cli.models.fetch_api_models", return_value=None):
        result = validate_requested_model(
            "glm-5.3",
            "zai",
            api_key="test-key",
            base_url="https://api.z.ai/api/coding/paas/v4",
        )

    assert result == {
        "accepted": True,
        "persist": True,
        "recognized": True,
        "message": None,
    }
