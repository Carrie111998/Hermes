"""agent/grok_session.py — CDP page-context scrape of grok.com rate limits.

The CDP/websocket layer is mocked; only discovery, targeting, parse and
degradation logic run for real.
"""

import json
from datetime import timezone

import agent.grok_session as grok_session
import hermes_cli.browser_connect as browser_connect


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
    monkeypatch.setattr(grok_session, "_usable_grok_target", lambda http: (None, target_ws))
    monkeypatch.setattr(
        browser_connect, "cdp_call",
        lambda url, method, params=None, *, timeout: _evaluate_stub(ws),
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
        grok_session, "_usable_grok_target", lambda http: (None, "ws://t")
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
    monkeypatch.setattr(grok_session, "_find_grok_targets", lambda http: [])
    monkeypatch.setattr(grok_session, "_new_grok_target", lambda http: None)

    assert grok_session.fetch_grok_rate_limits() is None


def test_fetch_none_when_endpoint_rejects(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(grok_session, "_usable_grok_target", lambda http: (None, "ws://t"))
    monkeypatch.setattr(
        grok_session, "_evaluate_rate_limits",
        lambda url, *, timeout: None,
    )

    assert grok_session.fetch_grok_rate_limits() is None


def test_fetch_none_on_exception(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(grok_session, "_usable_grok_target", lambda http: (None, "ws://t"))

    def throw(url, *, timeout):
        raise RuntimeError("ws gone")

    monkeypatch.setattr(grok_session, "_evaluate_rate_limits", throw)

    # fetch_grok_rate_limits must swallow transport errors, never raise.
    result = grok_session.fetch_grok_rate_limits()
    assert result is None or isinstance(result, tuple)


# ---------------------------------------------------------------------------
# Frozen-tab handling (2026-08-25)
#
# The xai row sat "unavailable" for 39h with Chrome UP and a logged-in
# grok.com tab present. Chrome had FROZEN that background tab: it still
# appeared in /json/list with a valid webSocketDebuggerUrl and the websocket
# still connected, but Runtime.evaluate never returned -- even a synchronous
# `location.href` timed out. _find_grok_target returned it unconditionally and
# the one retry hit the same dead tab. Proven live: a freshly opened tab got
# HTTP 200 {"windowSizeSeconds":14400,"remainingQueries":2,"totalQueries":2}
# from the same endpoint with the same session, seconds later.


def test_responsiveness_probe_is_false_when_evaluate_hangs(monkeypatch):
    """A frozen renderer connects fine and never answers -- only an evaluation tells."""
    def hang(url, method, params=None, *, timeout):
        raise TimeoutError("Connection timed out")

    # The probe is shared with gemini_session and lives in browser_connect, so
    # the round trip it stands on is patched there.
    monkeypatch.setattr(browser_connect, "cdp_call", hang)
    assert grok_session._target_is_responsive("ws://frozen") is False


def test_responsiveness_probe_is_true_when_evaluate_answers(monkeypatch):
    seen = {}

    def answer(url, method, params=None, *, timeout):
        seen["method"] = method
        return {"result": {"value": 1}}

    monkeypatch.setattr(browser_connect, "cdp_call", answer)
    assert grok_session._target_is_responsive("ws://live") is True
    # It must be a real evaluation -- a mere connection proves nothing.
    assert seen["method"] == "Runtime.evaluate"


def test_frozen_existing_tab_is_skipped_for_a_fresh_one(monkeypatch):
    """The regression: a frozen tab must not be chosen just because it exists."""
    monkeypatch.setattr(
        grok_session, "_find_grok_targets", lambda http: [("frozen-id", "ws://frozen")]
    )
    monkeypatch.setattr(
        grok_session, "_target_is_responsive",
        lambda ws, timeout=None: ws != "ws://frozen",
    )
    monkeypatch.setattr(
        grok_session, "_new_grok_target", lambda http: ("fresh-id", "ws://fresh")
    )

    opened_id, ws = grok_session._usable_grok_target("http://127.0.0.1:9222")

    assert ws == "ws://fresh"
    assert opened_id == "fresh-id"      # so the caller knows to close it


def test_a_responsive_user_tab_is_reused_and_never_closed(monkeypatch):
    """The ordinary case must touch nothing: no tab opened, so none closed."""
    monkeypatch.setattr(
        grok_session, "_find_grok_targets", lambda http: [("theirs", "ws://theirs")]
    )
    monkeypatch.setattr(
        grok_session, "_target_is_responsive", lambda ws, timeout=None: True
    )

    def must_not_open(http):
        raise AssertionError("opened a tab despite a responsive one existing")

    monkeypatch.setattr(grok_session, "_new_grok_target", must_not_open)

    opened_id, ws = grok_session._usable_grok_target("http://127.0.0.1:9222")

    assert ws == "ws://theirs"
    assert opened_id is None


def test_a_tab_we_opened_is_closed_after_a_successful_fetch(monkeypatch):
    closed = []
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url",
        lambda port, timeout=None: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        grok_session, "_usable_grok_target", lambda http: ("mine", "ws://mine")
    )
    monkeypatch.setattr(
        grok_session, "_evaluate_rate_limits",
        lambda url, *, timeout: [
            {"remainingQueries": 2, "totalQueries": 2, "windowSizeSeconds": 14400}
        ],
    )
    monkeypatch.setattr(
        grok_session, "_close_target", lambda http, tid: closed.append(tid)
    )

    result = grok_session.fetch_grok_rate_limits()

    assert result[:2] == (2.0, 2.0)
    assert closed == ["mine"]


def test_a_tab_we_opened_is_closed_even_when_the_fetch_raises(monkeypatch):
    """Otherwise a persistently failing provider leaks 288 tabs a day."""
    closed = []
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url",
        lambda port, timeout=None: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        grok_session, "_usable_grok_target", lambda http: ("mine", "ws://mine")
    )

    def throw(url, *, timeout):
        raise RuntimeError("ws gone")

    monkeypatch.setattr(grok_session, "_evaluate_rate_limits", throw)
    monkeypatch.setattr(
        grok_session, "_close_target", lambda http, tid: closed.append(tid)
    )

    assert grok_session.fetch_grok_rate_limits() is None
    assert closed == ["mine"]


def test_the_users_tab_is_never_closed(monkeypatch):
    closed = []
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url",
        lambda port, timeout=None: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        grok_session, "_usable_grok_target", lambda http: (None, "ws://theirs")
    )
    monkeypatch.setattr(
        grok_session, "_evaluate_rate_limits",
        lambda url, *, timeout: [{"remainingQueries": 1, "totalQueries": 4}],
    )
    monkeypatch.setattr(
        grok_session, "_close_target", lambda http, tid: closed.append(tid)
    )

    assert grok_session.fetch_grok_rate_limits() is not None
    assert closed == []


def test_all_tabs_frozen_and_open_fails_degrades_to_none(monkeypatch):
    monkeypatch.setattr(
        grok_session, "discover_local_cdp_url",
        lambda port, timeout=None: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        grok_session, "_find_grok_targets", lambda http: [("frozen", "ws://frozen")]
    )
    monkeypatch.setattr(
        grok_session, "_target_is_responsive", lambda ws, timeout=None: False
    )
    monkeypatch.setattr(grok_session, "_new_grok_target", lambda http: None)

    assert grok_session.fetch_grok_rate_limits() is None


def test_find_targets_returns_ids_and_skips_non_grok_pages():
    """Shape guard: the id is what makes closing possible."""
    payload = [
        {"type": "page", "url": "https://mail.google.com/", "id": "a",
         "webSocketDebuggerUrl": "ws://a"},
        {"type": "page", "url": "https://grok.com/chat", "id": "b",
         "webSocketDebuggerUrl": "ws://b"},
        # No id -> unclosable, so unusable.
        {"type": "page", "url": "https://grok.com/", "webSocketDebuggerUrl": "ws://c"},
        {"type": "iframe", "url": "https://grok.com/", "id": "d",
         "webSocketDebuggerUrl": "ws://d"},
    ]

    import agent.grok_session as gs
    import urllib.request
    from contextlib import contextmanager

    @contextmanager
    def fake_urlopen(url, timeout=None):
        class R:
            def read(self_inner):
                return json.dumps(payload).encode()
        yield R()

    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_urlopen
    try:
        found = gs._find_grok_targets("http://127.0.0.1:9222")
    finally:
        urllib.request.urlopen = orig

    assert found == [("b", "ws://b")]
