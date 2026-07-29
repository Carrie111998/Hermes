"""Regression tests for Gemini/Google model catalog discovery (issue #73825).

The Gemini provider model list previously included 3.x model IDs
(gemini-3.1-pro-preview, gemini-3.6-flash, etc.) that don't exist on
Google's direct API -- only on OpenRouter. Both the hand-curated static
list AND the models.dev merge (_MODELS_DEV_PREFERRED included "gemini"/
"google") contained these fictional entries, so selecting one silently
404'd with no indication to the user.

provider_model_ids("gemini") now queries Google's live
/v1beta/openai/models endpoint (Gemini's documented OpenAI-compatible
surface, which returns the same {"data": [{"id": ...}]} shape
fetch_api_models() already parses) first, and only falls back to a
corrected, verified static list -- never to models.dev's third-party
"google" catalog.
"""
from __future__ import annotations

from hermes_cli.models import _MODELS_DEV_PREFERRED, _PROVIDER_MODELS, provider_model_ids


class TestGeminiLiveModelDiscovery:
    def test_prefers_live_api_over_static_list(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {
                "provider": provider_id,
                "api_key": "AIzaFakeKey",
                "base_url": "",
                "source": "GEMINI_API_KEY",
            },
        )

        def _fake_fetch(api_key, base_url):
            assert base_url == "https://generativelanguage.googleapis.com/v1beta/openai"
            assert api_key == "AIzaFakeKey"
            return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"]

        monkeypatch.setattr("hermes_cli.models.fetch_api_models", _fake_fetch)

        assert provider_model_ids("gemini") == [
            "gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash",
        ]

    def test_falls_back_to_corrected_static_list_when_live_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {
                "provider": provider_id,
                "api_key": "AIzaFakeKey",
                "base_url": "",
                "source": "GEMINI_API_KEY",
            },
        )
        monkeypatch.setattr(
            "hermes_cli.models.fetch_api_models", lambda api_key, base_url: None
        )

        result = provider_model_ids("gemini")
        assert result == list(_PROVIDER_MODELS["gemini"])

    def test_falls_back_to_static_list_when_no_api_key(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {"provider": provider_id, "api_key": "", "base_url": "", "source": ""},
        )
        result = provider_model_ids("gemini")
        assert result == list(_PROVIDER_MODELS["gemini"])

    def test_static_fallback_contains_no_fictional_3x_models(self):
        """The curated fallback must only list models verified to exist on
        Google's own API -- no 3.x IDs that only exist on OpenRouter."""
        curated = _PROVIDER_MODELS["gemini"]
        for model_id in curated:
            assert not model_id.startswith("gemini-3"), (
                f"{model_id!r} is a fictional/OpenRouter-only model that "
                f"404s on Google's direct API"
            )

    def test_static_fallback_contains_real_verified_models(self):
        """Sanity: the corrected list should contain the models the issue
        reporter verified exist on Google's live API."""
        curated = set(_PROVIDER_MODELS["gemini"])
        for expected in ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"):
            assert expected in curated

    def test_gemini_not_in_models_dev_preferred(self):
        """gemini/google must not be merged with models.dev's third-party
        catalog anymore -- provider_model_ids() has its own dedicated
        live-fetch + verified-fallback branch that always returns before
        reaching that merge, so leaving these in the set would be
        misleading dead code."""
        assert "gemini" not in _MODELS_DEV_PREFERRED
        assert "google" not in _MODELS_DEV_PREFERRED

    def test_google_alias_normalizes_to_gemini_branch(self, monkeypatch):
        """normalize_provider() canonicalizes 'google' to 'gemini' before
        provider_model_ids() runs, so passing either must hit the same
        live-fetch path, not the (now-removed) models.dev merge."""
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {"provider": provider_id, "api_key": "", "base_url": "", "source": ""},
        )
        result = provider_model_ids("google")
        assert result == list(_PROVIDER_MODELS["gemini"])

    def test_live_fetch_exception_falls_back_gracefully(self, monkeypatch):
        """A credential-resolution or network exception during the live
        fetch must not propagate -- must degrade to the static fallback."""
        def _raise(*a, **k):
            raise RuntimeError("network unreachable")

        monkeypatch.setattr("hermes_cli.auth.resolve_api_key_provider_credentials", _raise)

        result = provider_model_ids("gemini")  # must not raise
        assert result == list(_PROVIDER_MODELS["gemini"])
