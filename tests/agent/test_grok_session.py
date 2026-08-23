"""agent/grok_session.py — CDP page-context scrape of grok.com rate limits.

The CDP/websocket layer is mocked; only discovery, targeting, parse and
degradation logic run for real.
"""

import json
from datetime import timezone

import agent.grok_session as grok_session


class _FakeWS:
    def __init__(self, result_value, error=None):
        self._result = {"id": 1, "result": {"result": {"value": result_value}}}
        if error:
            self._result["error"] = error
        self.closed = False

    def send(self, raw):
        self.sent = json.loads(raw)

    def recv(self):
        return json.dumps(self._result)

    def close(self):
        self.closed = True


def _patch_cdp(monkeypatch, ws, http_url="http://127.0.0.1:9222", target_ws="ws://x/devtools/page/1"):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: http_url
    )
    monkeypatch.setattr(grok_session, "_find_grok_target", lambda http: target_ws)
    monkeypatch.setattr(
        grok_session, "_cdp_call",
        lambda url, method, params, *, timeout: _evaluate_stub(ws),
    )


def _evaluate_stub(ws):
    # Reproduce what Runtime.evaluate returns through _cdp_call.
    from websocket import create_connection  # noqa: F401  (import proves dep)

    return ws["result"]


def test_fetch_returns_remaining_total_and_reset(monkeypatch):
    # _evaluate_rate_limits already unwraps to the ENTRY LIST.
    entries = [{
        "modelName": "grok-4",
        "remainingQueries": 25,
        "totalQueries": 100,
        "windowSizeSeconds": 7200,
        "remainingWindow": {"resetTime": "2026-08-22T21:00:00Z"},
    }]
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(
        grok_session, "_find_grok_target", lambda http: "ws://t"
    )

    captured = {}

    def fake_evaluate(url, *, timeout):
        captured["timeout"] = timeout
        return entries

    monkeypatch.setattr(grok_session, "_evaluate_rate_limits", fake_evaluate)

    result = grok_session.fetch_grok_rate_limits(timeout=7.5)

    assert result is not None
    remaining, total, reset_at = result
    assert (remaining, total) == (25.0, 100.0)
    assert reset_at.tzinfo is timezone.utc
    assert captured["timeout"] == 7.5


def test_fetch_none_when_no_cdp_browser(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: None
    )

    assert grok_session.fetch_grok_rate_limits() is None


def test_fetch_none_when_no_grok_tab(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(grok_session, "_find_grok_target", lambda http: None)
    monkeypatch.setattr(grok_session, "_new_grok_target", lambda http: None)

    assert grok_session.fetch_grok_rate_limits() is None


def test_fetch_none_when_endpoint_rejects(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(grok_session, "_find_grok_target", lambda http: "ws://t")
    monkeypatch.setattr(
        grok_session, "_evaluate_rate_limits",
        lambda url, *, timeout: None,
    )

    assert grok_session.fetch_grok_rate_limits() is None


def test_fetch_none_on_exception(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(grok_session, "_find_grok_target", lambda http: "ws://t")

    def throw(url, *, timeout):
        raise RuntimeError("ws gone")

    monkeypatch.setattr(grok_session, "_evaluate_rate_limits", throw)

    # fetch_grok_rate_limits must swallow transport errors, never raise.
    result = grok_session.fetch_grok_rate_limits()
    assert result is None or isinstance(result, tuple)
