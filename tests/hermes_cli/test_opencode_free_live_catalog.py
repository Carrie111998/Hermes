"""Live-catalog regression coverage for the keyless OpenCode Free provider."""

from unittest.mock import patch


def test_provider_model_ids_filters_live_catalog_to_keyless_models():
    from hermes_cli import models

    live = [
        "paid-model",
        "laguna-s-2.1-free",
        "opencode-free/nemotron-3-ultra-free",
        "laguna-s-2.1-free",
        "big-pickle",  # live-listed is not enough: this is not probe-verified
    ]
    with patch.object(models, "fetch_api_models", return_value=live) as fetch:
        result = models.provider_model_ids("opencode-free", force_refresh=True)

    assert result == ["laguna-s-2.1-free", "nemotron-3-ultra-free"]
    fetch.assert_called_once_with(
        None,
        models._OPENCODE_ZEN_FREE_BASE_URL,
        headers=models.opencode_zen_free_headers(),
    )


def test_provider_model_ids_treats_reachable_empty_free_catalog_as_authoritative():
    from hermes_cli import models

    with patch.object(models, "fetch_api_models", return_value=["paid-model"]):
        assert models.provider_model_ids("opencode-free") == []


def test_provider_model_ids_uses_curated_floor_when_live_fetch_fails():
    from hermes_cli import models

    with patch.object(models, "fetch_api_models", return_value=None):
        assert models.provider_model_ids("opencode-free") == list(
            models._PROVIDER_MODELS["opencode-free"]
        )


def test_cached_provider_catalog_avoids_reprobing_within_ttl(tmp_path, monkeypatch):
    from hermes_cli import models

    monkeypatch.setattr(models, "_provider_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(models, "_credential_fingerprint", lambda provider: "keyless-fp")
    with patch.object(
        models, "fetch_api_models", return_value=["fresh-model-free"]
    ) as fetch:
        assert models.cached_provider_model_ids(
            "opencode-free", force_refresh=True
        ) == ["fresh-model-free"]
        assert models.cached_provider_model_ids("opencode-free") == [
            "fresh-model-free"
        ]

    fetch.assert_called_once()


def test_runtime_routing_uses_cached_live_catalog(tmp_path, monkeypatch):
    from hermes_cli import models

    monkeypatch.setattr(models, "_provider_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(models, "_credential_fingerprint", lambda provider: "keyless-fp")
    models._save_provider_models_cache(
        {
            "opencode-free": {
                "fp": "keyless-fp",
                "at": models.time.time(),
                "models": ["new-rotating-model-free"],
            }
        }
    )

    assert models.opencode_zen_free_runtime(
        "opencode-zen", "new-rotating-model-free"
    ) is not None
    assert models.opencode_zen_free_runtime(
        "opencode-zen", "x-preview-f-free"
    ) is None


def test_runtime_routing_falls_back_to_curated_catalog_on_corrupt_cache(
    tmp_path, monkeypatch
):
    from hermes_cli import models

    monkeypatch.setattr(models, "_provider_models_cache_path", lambda: tmp_path / "models.json")
    monkeypatch.setattr(models, "_credential_fingerprint", lambda provider: "keyless-fp")
    models._save_provider_models_cache(
        {"opencode-free": {"fp": "wrong", "at": "bad", "models": []}}
    )

    curated = models._PROVIDER_MODELS["opencode-free"][0]
    assert models.opencode_zen_free_runtime("opencode-zen", curated) is not None
