"""Regression coverage for provider model-cache fallback provenance."""

from copy import deepcopy

from hermes_cli import models as models_mod


def test_kimi_coding_fallback_keeps_current_long_context_model():
    assert "k3-256k" in models_mod._PROVIDER_MODELS["kimi-coding"]


def test_profile_fallback_is_marked_non_cacheable(monkeypatch):
    import providers
    from hermes_cli import auth

    class FakeProfile:
        auth_type = "api_key"
        base_url = "https://api.kimi.com/coding/v1"
        fallback_models = ("fallback-only",)

        @staticmethod
        def fetch_models(*, api_key, base_url):
            assert api_key == "test-key"
            assert base_url == "https://api.kimi.com/coding/v1"
            return None

    monkeypatch.setattr(providers, "get_provider_profile", lambda _slug: FakeProfile())
    monkeypatch.setattr(
        auth,
        "resolve_api_key_provider_credentials",
        lambda _slug: {
            "api_key": "test-key",
            "base_url": "https://api.kimi.com/coding/v1",
        },
    )

    result = models_mod.provider_model_ids("kimi-coding", force_refresh=True)

    assert result == ["fallback-only"]
    assert getattr(result, "cacheable", True) is False


def test_successful_profile_discovery_remains_cacheable(monkeypatch):
    import providers
    from hermes_cli import auth

    class FakeProfile:
        auth_type = "api_key"
        base_url = "https://api.kimi.com/coding/v1"
        fallback_models = ("fallback-only",)

        @staticmethod
        def fetch_models(*, api_key, base_url):
            assert api_key == "test-key"
            assert base_url == "https://api.kimi.com/coding/v1"
            return ["kimi-for-coding", "k3", "k3-256k"]

    monkeypatch.setattr(providers, "get_provider_profile", lambda _slug: FakeProfile())
    monkeypatch.setattr(
        auth,
        "resolve_api_key_provider_credentials",
        lambda _slug: {
            "api_key": "test-key",
            "base_url": "https://api.kimi.com/coding/v1",
        },
    )

    result = models_mod.provider_model_ids("kimi-coding", force_refresh=True)

    assert "k3-256k" in result
    assert getattr(result, "cacheable", True) is True


def test_failed_refresh_preserves_stale_live_only_models(monkeypatch):
    stale_live = ["kimi-for-coding", "k3", "k3-256k"]
    fallback = models_mod._ProviderModelCatalog(
        list(models_mod._PROVIDER_MODELS["kimi-coding"]),
        cacheable=False,
    )
    cache = {
        "kimi-coding": {
            "fp": "same-credentials",
            "at": 1.0,
            "models": stale_live,
        }
    }
    saved = []

    monkeypatch.setattr(models_mod, "_load_provider_models_cache", lambda: deepcopy(cache))
    monkeypatch.setattr(
        models_mod,
        "_credential_fingerprint",
        lambda _provider: "same-credentials",
    )
    monkeypatch.setattr(
        models_mod,
        "provider_model_ids",
        lambda _provider, force_refresh=False: fallback,
    )
    monkeypatch.setattr(
        models_mod,
        "_save_provider_models_cache",
        lambda data: saved.append(deepcopy(data)),
    )

    result = models_mod.cached_provider_model_ids(
        "kimi-coding",
        force_refresh=True,
    )

    assert result == stale_live
    assert "k3-256k" in result
    assert saved == []


def test_fallback_is_rendered_but_not_cached_without_live_history(monkeypatch):
    fallback = models_mod._ProviderModelCatalog(
        list(models_mod._PROVIDER_MODELS["kimi-coding"]),
        cacheable=False,
    )
    saved = []

    monkeypatch.setattr(models_mod, "_load_provider_models_cache", lambda: {})
    monkeypatch.setattr(
        models_mod,
        "_credential_fingerprint",
        lambda _provider: "same-credentials",
    )
    monkeypatch.setattr(
        models_mod,
        "provider_model_ids",
        lambda _provider, force_refresh=False: fallback,
    )
    monkeypatch.setattr(
        models_mod,
        "_save_provider_models_cache",
        lambda data: saved.append(deepcopy(data)),
    )

    result = models_mod.cached_provider_model_ids(
        "kimi-coding",
        force_refresh=True,
    )

    assert result == list(fallback)
    assert saved == []


def test_successful_live_catalog_is_cached(monkeypatch):
    live = ["kimi-for-coding", "k3", "k3-256k"]
    saved = []

    monkeypatch.setattr(models_mod, "_load_provider_models_cache", lambda: {})
    monkeypatch.setattr(
        models_mod,
        "_credential_fingerprint",
        lambda _provider: "same-credentials",
    )
    monkeypatch.setattr(
        models_mod,
        "provider_model_ids",
        lambda _provider, force_refresh=False: live,
    )
    monkeypatch.setattr(
        models_mod,
        "_save_provider_models_cache",
        lambda data: saved.append(deepcopy(data)),
    )

    result = models_mod.cached_provider_model_ids(
        "kimi-coding",
        force_refresh=True,
    )

    assert result == live
    assert len(saved) == 1
    assert saved[0]["kimi-coding"]["fp"] == "same-credentials"
    assert saved[0]["kimi-coding"]["models"] == live
