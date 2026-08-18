"""Shared /browser connect Camofox detection and reversible backend state."""

from __future__ import annotations

import json
import os

from hermes_cli.browser_connect import (
    BROWSER_CONNECT_MODE_ENV,
    BROWSER_PREV_CAMOFOX_SET_ENV,
    BROWSER_PREV_CAMOFOX_URL_ENV,
    apply_browser_backend_override,
    detect_browser_connect_backend,
    get_browser_connect_override,
    normalize_browser_connect_endpoint,
    restore_browser_backend_override,
)


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def test_normalize_browser_connect_endpoint_adds_http_for_schemeless_localhost():
    assert normalize_browser_connect_endpoint("localhost:9377") == "http://localhost:9377"
    assert normalize_browser_connect_endpoint("http://localhost:9377/") == "http://localhost:9377"


def test_detect_browser_connect_backend_prefers_cdp_version(monkeypatch):
    def fake_urlopen(url, timeout=2.0):
        if url.endswith("/json/version"):
            return _FakeResponse(
                body=_json_body({"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc"})
            )
        raise AssertionError(f"unexpected probe {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    mode, endpoint = detect_browser_connect_backend("http://127.0.0.1:9222")
    assert mode == "cdp"
    assert endpoint == "http://127.0.0.1:9222"


def test_detect_browser_connect_backend_falls_back_to_camofox_health(monkeypatch):
    def fake_urlopen(url, timeout=2.0):
        if url.endswith("/json/version"):
            raise OSError("not cdp")
        if url.endswith("/health"):
            return _FakeResponse(body=_json_body({"ok": True}))
        raise AssertionError(f"unexpected probe {url}")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    mode, endpoint = detect_browser_connect_backend("localhost:9377")
    assert mode == "camofox"
    assert endpoint == "http://localhost:9377"


def test_detect_status_only_http_200_is_not_camofox(monkeypatch):
    """A probe stub that only exposes HTTP 200 (no JSON body) is unknown."""

    class _StatusOnly:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _StatusOnly())
    mode, _endpoint = detect_browser_connect_backend("http://127.0.0.1:9222")
    assert mode == "unknown"


def test_detect_devtools_websocket_is_cdp_without_probe(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not probe")),
    )
    mode, endpoint = detect_browser_connect_backend(
        "ws://127.0.0.1:9222/devtools/browser/abc"
    )
    assert mode == "cdp"
    assert endpoint.endswith("/devtools/browser/abc")


def test_apply_and_restore_preserves_identical_camofox_url(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv(BROWSER_CONNECT_MODE_ENV, raising=False)
    monkeypatch.delenv(BROWSER_PREV_CAMOFOX_URL_ENV, raising=False)
    monkeypatch.delenv(BROWSER_PREV_CAMOFOX_SET_ENV, raising=False)

    apply_browser_backend_override(mode="camofox", url="http://localhost:9377")

    assert get_browser_connect_override() == ("camofox", "http://localhost:9377")
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"
    assert os.environ.get(BROWSER_PREV_CAMOFOX_SET_ENV) == "1"
    assert os.environ.get(BROWSER_PREV_CAMOFOX_URL_ENV) == "http://localhost:9377"

    assert restore_browser_backend_override() == "camofox"
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"
    assert get_browser_connect_override() == ("", "")
    assert BROWSER_PREV_CAMOFOX_SET_ENV not in os.environ


def test_apply_and_restore_restores_previous_different_camofox_url(monkeypatch):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9999")
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv(BROWSER_CONNECT_MODE_ENV, raising=False)

    apply_browser_backend_override(mode="camofox", url="http://localhost:9377")
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"

    restore_browser_backend_override()
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9999"


def test_apply_and_restore_clears_camofox_when_none_was_configured(monkeypatch):
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv(BROWSER_CONNECT_MODE_ENV, raising=False)

    apply_browser_backend_override(mode="camofox", url="http://localhost:9377")
    assert os.environ.get("CAMOFOX_URL") == "http://localhost:9377"

    restore_browser_backend_override()
    assert "CAMOFOX_URL" not in os.environ
