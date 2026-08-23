"""agent/gemini_session.py — CDP response-interception scrape of AI Studio usage.

The CDP/websocket layer is mocked; only discovery, targeting, JSPB parsing
and degradation logic run for real.
"""

import agent.gemini_session as gs

# Real wire shapes captured 2026-08-23 from the live apikey page.
_USAGE_LIMITS_BODY = '[[["projects/612559014061",null,["USD",null,37474000],["USD","250"]]]]'
_IMPORTED_PROJECTS_BODY = '[null,null,null,null,[["projects/612559014061",1]]]'


def test_parse_usage_limits_happy_path():
    result = gs._parse_usage_limits(_USAGE_LIMITS_BODY)
    assert result == (14.9896, 250.0)


def test_parse_usage_limits_zero_budget_is_none():
    body = '[[["projects/x",null,["USD",null,100],["USD","0"]]]]'
    assert gs._parse_usage_limits(body) is None


def test_parse_usage_limits_garbage_is_none():
    assert gs._parse_usage_limits("not json") is None
    assert gs._parse_usage_limits("[]") is None
    assert gs._parse_usage_limits("[[null]]") is None


def test_parse_imported_projects():
    assert gs._parse_imported_projects(_IMPORTED_PROJECTS_BODY) == [
        "projects/612559014061"
    ]


class _FakeInterceptor:
    """Mimics _Interceptor: yields the two paused responses then idles."""

    def __init__(self, bodies):
        self._bodies = list(bodies)
        self.calls = []

    def call(self, method, params=None, *, timeout=10.0):
        self.calls.append(method)
        if method == "Fetch.getResponseBody":
            return {"body": self._bodies.pop(0), "base64Encoded": False}
        return {}

    def recv_event(self, seconds):
        if not self._bodies:
            return None  # idle -> loop times out
        tail = (
            "ListImportedProjects"
            if len(self._bodies) == 2
            else "BatchGetProjectUsageLimits"
        )
        return {
            "method": "Fetch.requestPaused",
            "params": {"requestId": "r1", "request": {"url": f"https://x/{tail}"}},
        }


class _DummyWS:
    def send(self, raw):
        pass

    def close(self):
        pass


def _patch_discovery(monkeypatch, *, http_url="http://127.0.0.1:9222", target="ws://t"):
    monkeypatch.setattr(
        gs, "discover_local_cdp_url", lambda port, timeout=None: http_url
    )
    monkeypatch.setattr(gs, "_find_aistudio_target", lambda http: target)


def test_fetch_returns_pct_and_budget(monkeypatch):
    _patch_discovery(monkeypatch)
    fake = _FakeInterceptor([_IMPORTED_PROJECTS_BODY, _USAGE_LIMITS_BODY])
    monkeypatch.setattr(gs, "_Interceptor", lambda ws: fake)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    # Processing two queued events takes ms; a short window suffices.
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 1.0)

    result = gs.fetch_gemini_budget_usage(timeout=5.0)
    assert result == (14.9896, 250.0)
    assert "Fetch.continueRequest" in fake.calls  # responses were released


def test_fetch_none_when_no_cdp_browser(monkeypatch):
    monkeypatch.setattr(
        gs, "discover_local_cdp_url", lambda port, timeout=None: None
    )
    assert gs.fetch_gemini_budget_usage() is None


def test_fetch_none_when_no_tab(monkeypatch):
    _patch_discovery(monkeypatch, target=None)
    monkeypatch.setattr(gs, "_new_aistudio_target", lambda http: None)
    assert gs.fetch_gemini_budget_usage() is None


def test_fetch_none_when_rpc_never_fires(monkeypatch):
    _patch_discovery(monkeypatch)

    class _Idle(_FakeInterceptor):
        def recv_event(self, seconds):
            return None

    monkeypatch.setattr(gs, "_Interceptor", lambda ws: _Idle([]))
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)
    assert gs.fetch_gemini_budget_usage() is None


def test_fetch_none_on_transport_error(monkeypatch):
    _patch_discovery(monkeypatch)

    class _Boom(_FakeInterceptor):
        def call(self, method, params=None, *, timeout=10.0):
            raise RuntimeError("ws gone")

        def recv_event(self, seconds):
            raise RuntimeError("ws gone")

    monkeypatch.setattr(gs, "_Interceptor", lambda ws: _Boom([]))
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.2)
    assert gs.fetch_gemini_budget_usage() is None
