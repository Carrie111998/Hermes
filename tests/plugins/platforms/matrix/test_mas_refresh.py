"""MAS OAuth2 + legacy refresh tests for the Matrix adapter (#94096).

matrix.org runs MAS: the legacy ``/_matrix/client/v3/refresh`` endpoint
rejects MAS-issued tokens (live-verified), so the adapter must fall back to
the OAuth2 token endpoint from OIDC discovery. Also pins the bounded
attempt count so a rejecting server can't drive an endless rotate loop.
"""
import asyncio
import json
import types

import pytest

from plugins.platforms.matrix import adapter as matrix_adapter


class _FakeApi:
    def __init__(self):
        self.token = "old-access"
        self.legacy_calls = 0

    async def request(self, method, path, body):
        self.legacy_calls += 1
        raise RuntimeError("M_UNKNOWN_TOKEN Invalid refresh token")


def _make_adapter(client_id="my-client"):
    a = object.__new__(matrix_adapter.MatrixAdapter)
    a._refresh_token = "rt-1"
    a._homeserver = "https://matrix.org"
    a._access_token = "old-access"
    a._refresh_lock = asyncio.Lock()
    a._oidc_client_id = client_id
    a._oidc_token_endpoint = None
    a._oidc_hint_logged = False
    a._refresh_attempts = 0
    return a


@pytest.mark.asyncio
async def test_legacy_refresh_success_applies_tokens():
    a = _make_adapter()
    api = _FakeApi()

    async def req(method, path, body):
        return {"access_token": "acc-2", "refresh_token": "rt-2"}

    api.request = req
    assert await a._refresh_access_token(api, reason="test") is True
    assert a._access_token == "acc-2"
    assert api.token == "acc-2"
    assert a._refresh_token == "rt-2"


class _Ctx:
    """Context-manager wrapper for stubbed urlopen responses."""

    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_legacy_rejection_falls_back_to_mas_oauth(monkeypatch):
    import urllib.request as _ureq

    a = _make_adapter(client_id="pub-client")
    api = _FakeApi()
    captured = {}

    def routed(req, timeout=10):
        url = req if isinstance(req, str) else req.full_url
        captured["urls"] = captured.get("urls", []) + [url]
        if "openid-configuration" in url:
            return _Ctx(
                {"token_endpoint": "https://account.matrix.org/oauth2/token"}
            )
        captured["token_url"] = url
        captured["body"] = req.data.decode()
        return _Ctx({
            "access_token": "mas-acc",
            "refresh_token": "mas-rt",
            "expires_in": 14400,
        })

    monkeypatch.setattr(_ureq, "urlopen", routed, raising=True)

    assert await a._refresh_access_token(api, reason="test") is True
    assert any("openid-configuration" in u for u in captured["urls"])
    assert captured["token_url"] == "https://account.matrix.org/oauth2/token"
    assert "grant_type=refresh_token" in captured["body"]
    assert "client_id=pub-client" in captured["body"]
    assert a._access_token == "mas-acc"
    assert a._refresh_token == "mas-rt"
    assert api.token == "mas-acc"


@pytest.mark.asyncio
async def test_mas_path_requires_client_id(monkeypatch):
    import urllib.request as _ureq

    a = _make_adapter(client_id="")
    api = _FakeApi()
    monkeypatch.delenv("MATRIX_OIDC_CLIENT_ID", raising=False)
    monkeypatch.setattr(
        _ureq, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no network")),
        raising=True,
    )
    assert await a._refresh_access_token(api, reason="test") is False


@pytest.mark.asyncio
async def test_refresh_attempts_bounded(monkeypatch):
    import urllib.request as _ureq

    a = _make_adapter(client_id="pub")
    api = _FakeApi()
    monkeypatch.setenv("MATRIX_OIDC_CLIENT_ID", "pub")

    def always_fail(req, timeout=10):
        raise RuntimeError("M_UNKNOWN_TOKEN Invalid refresh token")

    monkeypatch.setattr(_ureq, "urlopen", always_fail, raising=True)

    for _ in range(matrix_adapter._MAX_REFRESH_ATTEMPTS + 3):
        ok = await a._refresh_access_token(api, reason="test")
        assert ok is False

    # Cap reached: further calls fail fast without touching the network.
    calls_after_cap = api.legacy_calls
    assert await a._refresh_access_token(api, reason="test") is False
    assert api.legacy_calls == calls_after_cap
    assert a._refresh_attempts >= matrix_adapter._MAX_REFRESH_ATTEMPTS
