"""Unit tests for the Telnyx provider profile.

Pins the profile's contract without going live: identity, catalog-ID model
defaults, the load-bearing ``default_max_tokens=None`` (Telnyx error 10015
rejects an output cap combined with function tools on hosted models), and
the task-filtered ``fetch_models`` override.
"""

from __future__ import annotations

import io
import json
from contextlib import contextmanager
from unittest.mock import patch

import pytest


@pytest.fixture
def telnyx_profile():
    """Resolve the registered Telnyx profile through the real discovery path."""
    # Importing model_tools triggers plugin discovery, registering the profile.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("telnyx")
    assert profile is not None, "telnyx provider profile must be registered"
    return profile


class TestTelnyxIdentity:
    def test_core_fields(self, telnyx_profile):
        p = telnyx_profile
        assert p.name == "telnyx"
        assert p.auth_type == "api_key"
        assert p.base_url == "https://api.telnyx.com/v2/ai/openai"
        assert "TELNYX_API_KEY" in p.env_vars
        assert "TELNYX_BASE_URL" not in p.env_vars

    def test_display_metadata_present(self, telnyx_profile):
        # Picker copy stays non-empty rather than pinning exact wording.
        assert telnyx_profile.display_name
        assert telnyx_profile.description
        assert telnyx_profile.signup_url.startswith("https://")

    def test_no_partner_attribution_headers(self, telnyx_profile):
        assert "HTTP-Referer" not in telnyx_profile.default_headers
        assert "X-Title" not in telnyx_profile.default_headers


class TestTelnyxMaxTokensContract:
    def test_no_default_output_cap(self, telnyx_profile):
        """Telnyx error 10015 rejects max_tokens + function tools on all
        hosted models, and no catalog metadata predicts which models are
        affected. The profile must never volunteer a cap — only an explicit
        user ``agent.max_tokens`` may send one."""
        assert telnyx_profile.default_max_tokens is None
        assert telnyx_profile.get_max_tokens("moonshotai/Kimi-K3") is None


class TestTelnyxModelDefaults:
    """Defaults must be live catalog IDs (``org/model`` form)."""

    def test_aux_model_is_catalog_id(self, telnyx_profile):
        aux = telnyx_profile.default_aux_model
        assert aux and "/" in aux, aux

    def test_fallback_models_are_catalog_ids(self, telnyx_profile):
        assert telnyx_profile.fallback_models, "expected curated fallbacks"
        for model in telnyx_profile.fallback_models:
            assert "/" in model, model


@contextmanager
def _fake_models_response(rows):
    payload = json.dumps({"data": rows}).encode()

    @contextmanager
    def _fake_open(req, timeout=None):
        yield io.BytesIO(payload)

    with patch("hermes_cli.urllib_security.open_credentialed_url", _fake_open):
        yield


class TestTelnyxFetchModels:
    def test_filters_to_text_generation_both_spellings(self, telnyx_profile):
        """The catalog labels hosted models ``text-generation`` and proxied
        frontier routes ``text generation`` (with a space) — both must pass;
        non-text tasks must not reach the chat picker."""
        rows = [
            {"id": "moonshotai/Kimi-K3", "task": "text-generation"},
            {"id": "openai/gpt-5", "task": "text generation"},
            {"id": "some/embedder", "task": "embedding"},
            {"id": "future/no-task-field"},
        ]
        with _fake_models_response(rows):
            got = telnyx_profile.fetch_models(api_key="test-key")
        assert got == ["moonshotai/Kimi-K3", "openai/gpt-5", "future/no-task-field"]

    def test_fetch_failure_returns_none(self, telnyx_profile):
        @contextmanager
        def _boom(req, timeout=None):
            raise OSError("connection refused")
            yield  # pragma: no cover

        with patch("hermes_cli.urllib_security.open_credentialed_url", _boom):
            assert telnyx_profile.fetch_models(api_key="test-key") is None
