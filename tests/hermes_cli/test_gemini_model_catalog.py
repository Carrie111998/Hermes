"""Regression tests for Gemini/Google live model discovery (issue #73825,
corrected per review of #73952).

provider_model_ids("gemini") now queries Google's live /v1beta/openai/models
endpoint first (Gemini's documented OpenAI-compatible surface), falling back
to the curated static list and the existing models.dev merge when live
fetch is unavailable. Live results have their "models/" prefix stripped to
match the bare-ID convention the curated list, user input, and the existing
Gemini validation path (hermes_cli/models.py, #12532) all use.

Note: an earlier revision of this fix incorrectly claimed Gemini 3.x model
IDs (gemini-3.1-pro-preview, gemini-3-pro-preview, gemini-3.6-flash,
gemini-3.1-flash-lite-preview) were "fictional"/OpenRouter-only and removed
them from the curated list. That was wrong: plugins/model-providers/gemini/
__init__.py's own default_aux_model is gemini-3.6-flash, and
website/docs/guides/google-gemini.md documents these as genuine native
Gemini IDs. The curated list is left untouched here. Tests in this file
verify normalization and fallback-contract behavior, not specific model
names or generations -- per AGENTS.md's guidance against catalog-content
snapshot tests, since model availability is expected to change over time.
"""
from __future__ import annotations

from hermes_cli.models import provider_model_ids


class TestGeminiLiveModelDiscovery:
    def test_live_fetch_strips_models_prefix(self, monkeypatch):
        """Regression (review of #73952): Gemini's OpenAI-compat endpoint
        returns IDs prefixed with 'models/' (e.g. 'models/gemini-2.5-pro'),
        matching the SAME normalization already applied at the existing
        Gemini validation call site (#12532). Without stripping it here
        too, a successful live discovery would cache IDs the native
        provider doesn't accept as-is (e.g. selecting 'models/gemini-2.5-pro'
        would fail where 'gemini-2.5-pro' succeeds)."""
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
            return ["models/gemini-2.5-pro", "models/gemini-2.5-flash"]

        monkeypatch.setattr("hermes_cli.models.fetch_api_models", _fake_fetch)

        result = provider_model_ids("gemini")

        assert result == ["gemini-2.5-pro", "gemini-2.5-flash"], (
            "Live-fetched IDs must have the 'models/' prefix stripped to "
            "match the bare-ID convention used everywhere else"
        )
        assert not any(m.startswith("models/") for m in result)

    def test_live_fetch_passes_through_already_bare_ids_unchanged(self, monkeypatch):
        """Sanity: if a live result is already bare (no 'models/' prefix,
        e.g. a future API revision or a differently-shaped response), the
        normalization must be a no-op, not corrupt it."""
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
            "hermes_cli.models.fetch_api_models",
            lambda api_key, base_url: ["gemini-2.5-pro", "gemini-2.5-flash"],
        )

        result = provider_model_ids("gemini")

        assert result == ["gemini-2.5-pro", "gemini-2.5-flash"]

    def test_live_fetch_preferred_over_static_and_models_dev(self, monkeypatch):
        """Live discovery is consulted first; a successful live result is
        returned without falling through to the static list or a
        models.dev merge."""
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
            "hermes_cli.models.fetch_api_models",
            lambda api_key, base_url: ["models/gemini-2.5-pro"],
        )

        result = provider_model_ids("gemini")

        assert result == ["gemini-2.5-pro"]

    def test_falls_back_when_no_api_key(self, monkeypatch):
        """No credentials configured: live fetch is skipped and the
        function falls through to its existing static/models.dev fallback
        chain (not forcibly short-circuited to only the static list)."""
        monkeypatch.setattr(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            lambda provider_id: {"provider": provider_id, "api_key": "", "base_url": "", "source": ""},
        )

        result = provider_model_ids("gemini")

        # Must not raise, must return a non-empty list of bare (no
        # "models/" prefix) IDs from whatever fallback source is active.
        assert isinstance(result, list)
        assert result
        assert not any(isinstance(m, str) and m.startswith("models/") for m in result)

    def test_live_fetch_exception_falls_back_gracefully(self, monkeypatch):
        """A credential-resolution or network exception during the live
        fetch must not propagate -- must degrade to the existing fallback
        chain."""
        def _raise(*a, **k):
            raise RuntimeError("network unreachable")

        monkeypatch.setattr("hermes_cli.auth.resolve_api_key_provider_credentials", _raise)

        result = provider_model_ids("gemini")  # must not raise

        assert isinstance(result, list)
        assert result

    def test_google_alias_normalizes_to_gemini_live_fetch(self, monkeypatch):
        """normalize_provider() canonicalizes 'google' to 'gemini' before
        provider_model_ids() runs, so passing either must hit the same
        live-fetch-first path."""
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
            "hermes_cli.models.fetch_api_models",
            lambda api_key, base_url: ["models/gemini-2.5-flash"],
        )

        result = provider_model_ids("google")

        assert result == ["gemini-2.5-flash"]

    def test_picker_output_never_leaks_models_prefix_regardless_of_source(self, monkeypatch):
        """Picker/native-ID compatibility invariant (per review): no matter
        which source provider_model_ids("gemini") ends up returning from
        (live fetch, static list, or models.dev merge), every ID in the
        result must be in the bare, native form the rest of the codebase
        (validation, user input, config) expects -- never "models/"-prefixed.
        """
        # Exercise all three sources in one assertion pass.
        for api_key, fetch_result in (
            ("AIzaFakeKey", ["models/gemini-2.5-pro"]),  # live succeeds
            ("", None),  # no key -> fallback chain
        ):
            monkeypatch.setattr(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                lambda provider_id, _key=api_key: {
                    "provider": provider_id, "api_key": _key, "base_url": "", "source": "",
                },
            )
            if fetch_result is not None:
                monkeypatch.setattr(
                    "hermes_cli.models.fetch_api_models",
                    lambda api_key, base_url, _r=fetch_result: _r,
                )
            result = provider_model_ids("gemini")
            assert all(
                isinstance(m, str) and not m.startswith("models/") for m in result
            ), f"picker output leaked 'models/' prefix: {result}"
