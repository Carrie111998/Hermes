"""provider_model_ids() resolves named custom:<name> providers from config.

Regression: only the bare "custom" slug was resolved from config; every
named custom provider (custom:<name>) fell through to the empty static
catalog, so the model picker showed 0 models for configured, working
providers.
"""

from unittest.mock import patch

import yaml

from hermes_constants import get_hermes_home


def _write_provider_config(provider_name: str, base_url: str, key_env: str) -> None:
    config = {
        "providers": {
            provider_name: {
                "api_key": "${" + key_env + "}",
                "base_url": base_url,
            }
        }
    }
    (get_hermes_home() / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def test_named_custom_provider_resolves_config_base_url_and_key(monkeypatch):
    """A configured custom:<name> slug must resolve its provider entry from
    config (base_url + ${ENV}-expanded api_key) and fetch the live catalog,
    not fall through to the empty static table."""
    from hermes_cli import models as models_mod

    _write_provider_config("acme", "https://acme.example/v1", "ACME_TEST_API_KEY")
    monkeypatch.setenv("ACME_TEST_API_KEY", "sk-acme-test")

    captured = {}

    def _fake_fetch(api_key, base_url, api_mode=None, **kwargs):
        captured["api_key"] = api_key
        captured["base_url"] = base_url
        return ["acme-model-1"]

    with patch.object(models_mod, "fetch_api_models", _fake_fetch):
        ids = models_mod.provider_model_ids("custom:acme", force_refresh=True)

    assert ids == ["acme-model-1"]
    assert captured["base_url"] == "https://acme.example/v1"
    assert captured["api_key"] == "sk-acme-test"


def test_named_custom_provider_without_key_returns_empty_without_fetch(monkeypatch):
    """A named custom provider whose api_key cannot be resolved must not
    fetch and must not crash — it falls through to the (empty) static
    catalog, same as before for unconfigured providers."""

    from hermes_cli import models as models_mod

    def _fail_fetch(*args, **kwargs):
        raise AssertionError("fetch must not run without a resolvable api_key")

    _write_provider_config("nokey", "https://nokey.example/v1", "NOKEY_TEST_API_KEY")
    monkeypatch.delenv("NOKEY_TEST_API_KEY", raising=False)

    with patch.object(models_mod, "fetch_api_models", _fail_fetch):
        ids = models_mod.provider_model_ids("custom:nokey", force_refresh=True)

    assert ids == []
