"""Tests for per-provider ``extra_headers`` in providers / custom_providers config.

PR #3526 salvage — user-configurable extra HTTP headers on LLM API calls
(reverse proxies, gateways, custom auth such as Cloudflare Access tokens).
"""

import json

from hermes_cli.config import (
    _normalize_custom_provider_entry,
    apply_custom_provider_extra_headers_to_client_kwargs,
    get_custom_provider_extra_headers,
    normalize_extra_headers,
)
from hermes_cli import models as models_mod




def test_normalize_entry_keeps_extra_headers():
    normalized = _normalize_custom_provider_entry(
        {
            "name": "my-proxy",
            "base_url": "https://llm.internal.example.com/v1",
            "extra_headers": {"X-Custom-Auth": "tok", "X-Client-Name": "hermes"},
        }
    )
    assert normalized is not None
    assert normalized["extra_headers"] == {
        "X-Custom-Auth": "tok",
        "X-Client-Name": "hermes",
    }













def test_shared_base_url_matches_named_provider_identity():
    providers = [
        {
            "provider_key": "first",
            "name": "first",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "first"},
        },
        {
            "provider_key": "second",
            "name": "second",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "second"},
        },
    ]

    assert get_custom_provider_extra_headers(
        "https://shared.example.com/v1",
        custom_providers=providers,
        provider_key="custom:second",
    ) == {"X-Route": "second"}


def test_shared_base_url_matches_legacy_custom_provider_name():
    providers = [
        {
            "name": "first",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "first"},
        },
        {
            "name": "second",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "second"},
        },
    ]

    assert get_custom_provider_extra_headers(
        "https://shared.example.com/v1",
        custom_providers=providers,
        provider_key="custom:second",
    ) == {"X-Route": "second"}


def test_identity_does_not_fall_back_to_wrong_shared_url_entry():
    providers = [
        {
            "name": "first",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "first"},
        }
    ]

    assert get_custom_provider_extra_headers(
        "https://shared.example.com/v1",
        custom_providers=providers,
        provider_key="custom:missing",
    ) == {}


def test_apply_extra_headers_selects_named_provider_on_shared_route():
    client_kwargs = {"default_headers": {"X-Keep": "1"}}
    providers = [
        {
            "provider_key": "first",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "first"},
        },
        {
            "provider_key": "second",
            "base_url": "https://shared.example.com/v1",
            "extra_headers": {"X-Route": "second"},
        },
    ]

    apply_custom_provider_extra_headers_to_client_kwargs(
        client_kwargs,
        "https://shared.example.com/v1",
        custom_providers=providers,
        provider_key="custom:second",
    )
    assert client_kwargs["default_headers"] == {
        "X-Keep": "1",
        "X-Route": "second",
    }


def test_fetch_api_models_sends_extra_headers_to_models_probe(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "proxy-model"}]}).encode()

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = {
            key.lower(): value
            for key, value in request.header_items()
        }
        return FakeResponse()

    monkeypatch.setattr(models_mod, "_urlopen_model_catalog_request", fake_urlopen)

    models = models_mod.fetch_api_models(
        "proxy-key",
        "https://llm.internal.example.com/v1",
        headers={
            "sleeve-harness": "hermes",
            "sleeve-base-url": "http://localhost:8081/v1",
        },
    )

    assert models == ["proxy-model"]
    assert captured["url"] == "https://llm.internal.example.com/v1/models"
    assert captured["headers"]["authorization"] == "Bearer proxy-key"
    assert captured["headers"]["sleeve-harness"] == "hermes"
    assert captured["headers"]["sleeve-base-url"] == "http://localhost:8081/v1"
