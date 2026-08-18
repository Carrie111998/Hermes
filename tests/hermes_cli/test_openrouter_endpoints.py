"""Mocked contracts for profile-scoped OpenRouter endpoint discovery."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from hermes_cli import openrouter_endpoints
except ImportError:
    openrouter_endpoints = None


MODEL = "deepseek/deepseek-v4-flash"
MODEL_FREE = "deepseek/deepseek-chat:free"
API_KEY = "sk-or-test-secret-never-log"


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size=-1):
        return self._body


def _module():
    assert openrouter_endpoints is not None, (
        "openrouter_endpoints module is not implemented"
    )
    return openrouter_endpoints


@pytest.fixture(autouse=True)
def _clear_endpoint_cache():
    if openrouter_endpoints is not None:
        openrouter_endpoints._clear_cache_for_tests()
    yield
    if openrouter_endpoints is not None:
        openrouter_endpoints._clear_cache_for_tests()


@pytest.fixture
def upstream_payload():
    return {
        "data": {
            "id": MODEL,
            "endpoints": [
                {
                    "provider_name": "Baidu Qianfan",
                    "tag": "baidu",
                    "quantization": "fp8",
                    "status": "available",
                    "context_length": 131072,
                    "pricing": {
                        "prompt": "0.00000027",
                        "completion": "0.00000110",
                    },
                    "latency": 0.42,
                    "throughput": 88.5,
                    "uptime": 99.9,
                    "supported_parameters": ["tools", "temperature"],
                }
            ],
        }
    }


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        (MODEL, ("deepseek", "deepseek-v4-flash")),
        (MODEL_FREE, ("deepseek", "deepseek-chat:free")),
    ],
)
def test_validate_model_id_accepts_author_slug_and_preserves_suffix(model, expected):
    assert _module().validate_openrouter_model_id(model) == expected


@pytest.mark.parametrize(
    "model",
    [
        "",
        "deepseek",
        "/model",
        "author/",
        "author/model/extra",
        "../model",
        "author/..",
        "author/%2e%2e",
        "author\\model",
        "author/model?x=1",
        "a" * 257,
    ],
)
def test_validate_model_id_rejects_unsafe_or_oversized_values(model):
    with pytest.raises(ValueError):
        _module().validate_openrouter_model_id(model)


def test_fetch_constructs_encoded_url_and_uses_auth_header(
    monkeypatch, upstream_payload
):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return FakeResponse(upstream_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _module().fetch_openrouter_endpoints(
        MODEL_FREE,
        api_key=API_KEY,
        timeout=3.5,
        profile_id="profile-a",
    )

    assert seen == {
        "url": "https://openrouter.ai/api/v1/models/deepseek/deepseek-chat%3Afree/endpoints",
        "authorization": f"Bearer {API_KEY}",
        "timeout": 3.5,
    }
    assert result["model"] == MODEL_FREE
    assert result["cached"] is False


def test_normalizes_complete_endpoint(upstream_payload, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(upstream_payload),
    )

    result = _module().fetch_openrouter_endpoints(
        MODEL, api_key=API_KEY, profile_id="profile-a"
    )

    assert result["endpoints"] == [upstream_payload["data"]["endpoints"][0]]
    assert result["fetched_at"].endswith("Z")


def test_normalization_tolerates_missing_optional_metrics():
    raw = {
        "provider_name": "Baidu Qianfan",
        "tag": "baidu/fp8",
        "quantization": "fp8",
        "status": 0,
        "context_length": 131072,
        "pricing": {"prompt": "0.1", "completion": "0.2"},
        "supported_parameters": ["tools", "temperature"],
    }

    normalized = _module().normalize_openrouter_endpoint(raw)

    assert normalized["provider_name"] == "Baidu Qianfan"
    assert normalized["tag"] == "baidu/fp8"
    assert normalized["quantization"] == "fp8"
    assert normalized["status"] == 0
    assert normalized["context_length"] == 131072
    assert normalized["pricing"] == {"prompt": "0.1", "completion": "0.2"}
    assert normalized["supported_parameters"] == ["tools", "temperature"]
    assert "latency" not in normalized
    assert "throughput" not in normalized
    assert "uptime" not in normalized


def test_normalization_maps_live_metric_names_and_preserves_tag_suffix():
    raw = {
        "provider_name": "Baidu Qianfan",
        "tag": "baidu/fp8",
        "quantization": "fp8",
        "status": 0,
        "latency_last_30m": None,
        "throughput_last_30m": 91.2,
        "uptime_last_30m": 99.8,
        "pricing": {
            "prompt": "0.1",
            "completion": "0.2",
            "input_cache_read": "0.01",
            "discount": 0,
        },
    }

    normalized = _module().normalize_openrouter_endpoint(raw)

    assert normalized["tag"] == "baidu/fp8"
    assert normalized["status"] == 0
    assert normalized["latency"] is None
    assert normalized["throughput"] == 91.2
    assert normalized["uptime"] == 99.8
    assert normalized["pricing"]["input_cache_read"] == "0.01"


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_upstream_http_errors_are_classified(monkeypatch, status):
    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://openrouter.ai/redacted",
            status,
            "upstream error",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", raise_http)

    with pytest.raises(_module().OpenRouterEndpointError) as exc_info:
        _module().fetch_openrouter_endpoints(
            MODEL, api_key=API_KEY, profile_id="profile-a"
        )

    expected = status if status < 500 else 503
    assert exc_info.value.status_code == expected
    assert API_KEY not in str(exc_info.value)


@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timed out"),
        TimeoutError("timed out"),
        urllib.error.URLError("offline"),
    ],
)
def test_timeout_and_network_errors_are_recoverable(monkeypatch, error):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    )

    with pytest.raises(_module().OpenRouterEndpointError) as exc_info:
        _module().fetch_openrouter_endpoints(
            MODEL, api_key=API_KEY, profile_id="profile-a"
        )

    assert exc_info.value.status_code in {503, 504}
    assert exc_info.value.recoverable is True


def test_malformed_json_never_logs_authorization(monkeypatch, caplog):
    response = FakeResponse({})
    response._body = b"not-json"
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: response)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(_module().OpenRouterEndpointError) as exc_info,
    ):
        _module().fetch_openrouter_endpoints(
            MODEL, api_key=API_KEY, profile_id="profile-a"
        )

    assert exc_info.value.status_code == 502
    assert API_KEY not in caplog.text
    assert API_KEY not in str(exc_info.value)


def test_cache_hit_avoids_network_and_refresh_bypasses_cache(
    monkeypatch, upstream_payload
):
    calls = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(upstream_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    module = _module()

    first = module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="a")
    cached = module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="a")
    refreshed = module.fetch_openrouter_endpoints(
        MODEL, api_key=API_KEY, profile_id="a", refresh=True
    )

    assert calls == 2
    assert first["cached"] is False
    assert cached["cached"] is True
    assert refreshed["cached"] is False


def test_cache_is_isolated_by_profile_and_model(monkeypatch, upstream_payload):
    calls = 0

    def fake_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse(upstream_payload)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    module = _module()

    module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="a")
    module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="b")
    module.fetch_openrouter_endpoints(MODEL_FREE, api_key=API_KEY, profile_id="a")
    module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="a")

    assert calls == 3


@pytest.mark.parametrize("failure", [socket.timeout("slow"), 503])
def test_stale_cache_is_returned_on_timeout_or_5xx(
    monkeypatch, upstream_payload, failure
):
    module = _module()
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(upstream_payload),
    )
    module.fetch_openrouter_endpoints(MODEL, api_key=API_KEY, profile_id="a")

    def fail(*_args, **_kwargs):
        if isinstance(failure, int):
            raise urllib.error.HTTPError(
                "https://openrouter.ai/redacted", failure, "bad", None, None
            )
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail)
    result = module.fetch_openrouter_endpoints(
        MODEL, api_key=API_KEY, profile_id="a", refresh=True
    )

    assert result["cached"] is True
    assert result["stale"] is True


def test_web_route_is_profile_scoped_and_runs_fetch_off_event_loop(
    monkeypatch, tmp_path
):
    from hermes_cli import web_server

    main_thread = threading.get_ident()
    seen = {}

    @contextmanager
    def fake_scope(profile):
        seen["profile"] = profile
        yield tmp_path / profile

    def fake_fetch(model, **kwargs):
        seen["thread"] = threading.get_ident()
        seen["model"] = model
        seen.update(kwargs)
        return {"model": model, "endpoints": [], "cached": False}

    monkeypatch.setattr(web_server, "_profile_scope", fake_scope)
    monkeypatch.setattr(web_server, "load_env", lambda: {"OPENROUTER_API_KEY": API_KEY})
    monkeypatch.setattr(_module(), "fetch_openrouter_endpoints", fake_fetch)

    result = asyncio.run(
        web_server.get_openrouter_endpoints(
            model=MODEL, profile="profile-a", refresh=True
        )
    )

    assert result["model"] == MODEL
    assert seen["profile"] == "profile-a"
    assert seen["profile_id"] == str(tmp_path / "profile-a")
    assert seen["refresh"] is True
    assert seen["thread"] != main_thread


def test_web_route_loads_key_from_current_profile_env(monkeypatch, tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli import config, web_server

    profile_home = tmp_path / "profile-home"
    profile_home.mkdir()
    (profile_home / ".env").write_text(
        "OPENROUTER_API_KEY=test-profile-key\n", encoding="utf-8"
    )
    seen = {}

    def fake_fetch(model, **kwargs):
        seen.update(kwargs)
        return {"model": model, "endpoints": [], "cached": False}

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(_module(), "fetch_openrouter_endpoints", fake_fetch)
    config.invalidate_env_cache()
    token = set_hermes_home_override(str(profile_home))
    try:
        result = asyncio.run(
            web_server.get_openrouter_endpoints(
                model=MODEL, profile=None, refresh=False
            )
        )
    finally:
        reset_hermes_home_override(token)
        config.invalidate_env_cache()

    assert result["model"] == MODEL
    assert seen["api_key"] == "test-profile-key"
    assert seen["profile_id"] == str(profile_home)


def test_web_route_rejects_missing_profile_key(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from hermes_cli import web_server

    @contextmanager
    def fake_scope(_profile):
        yield Path(tmp_path)

    monkeypatch.setattr(web_server, "_profile_scope", fake_scope)
    monkeypatch.setattr(web_server, "load_env", lambda: {})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            web_server.get_openrouter_endpoints(
                model=MODEL, profile="profile-a", refresh=False
            )
        )

    assert exc_info.value.status_code == 401
    assert API_KEY not in str(exc_info.value.detail)
