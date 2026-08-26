"""agent/gemini_session.py — CDP response-interception scrape of AI Studio usage.

The CDP/websocket layer is mocked; only discovery, targeting, JSPB parsing
and degradation logic run for real.
"""

import json

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
    monkeypatch.setattr(gs, "_live_aistudio_target", lambda http: target)


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


# --- wedged-tab recovery -----------------------------------------------------
#
# Production incident 2026-08-23: the FIRST aistudio tab (_live_aistudio_target
# still picks the first RESPONSIVE one) wedged -- its page loaded but the lazy RPC chain never
# completed -- so five consecutive PT5M runs burned the full 30s settle window.
# A same-target retry is useless against a wedged tab; recovery requires a
# FRESH tab. These tests pin that behavior.


class _OnceIdle(_FakeInterceptor):
    """First attempt idles out (wedged tab); later attempts succeed.

    One instance per _attempt() -- fetch_gemini_budget_usage builds a fresh
    _Interceptor for each try, mirroring that here via _mint().
    """

    instances: list = []

    @classmethod
    def _mint(cls, ws):
        inst = cls([])
        cls.instances.append(inst)
        return inst

    def recv_event(self, seconds):
        if len(self.instances) == 1:
            return None  # first (wedged) attempt: idle -> timeout
        if not self._bodies:
            self._bodies = [_IMPORTED_PROJECTS_BODY, _USAGE_LIMITS_BODY]
        tail = (
            "ListImportedProjects"
            if len(self._bodies) == 2
            else "BatchGetProjectUsageLimits"
        )
        return {
            "method": "Fetch.requestPaused",
            "params": {"requestId": "r1", "request": {"url": f"https://x/{tail}"}},
        }


def _patch_fresh_tab(monkeypatch, ws="ws://fresh", tid="fresh-tab"):
    """Stub /json/new + /json/close so no real browser is touched.

    _new_aistudio_target yields (target_id, ws_url) -- grok_session's order.
    """
    monkeypatch.setattr(
        gs, "_new_aistudio_target", lambda http: (tid, ws)
    )
    closed = []
    monkeypatch.setattr(
        gs, "_close_target", lambda http, t: closed.append(t) or True
    )
    return closed


def test_retry_after_wedged_tab_uses_fresh_tab(monkeypatch):
    _OnceIdle.instances = []
    _patch_discovery(monkeypatch)
    closed = _patch_fresh_tab(monkeypatch)
    monkeypatch.setattr(gs, "_Interceptor", _OnceIdle._mint)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(gs, "_RETRY_BACKOFF_SECONDS", 0.01)

    result = gs.fetch_gemini_budget_usage(timeout=5.0, budget_seconds=60.0)
    # The fresh-tab attempt produced the snapshot...
    assert result == (14.9896, 250.0)
    # ...on a SECOND interceptor instance (first was the wedged tab)...
    assert len(_OnceIdle.instances) == 2
    # ...and the throwaway tab was closed afterwards.
    assert closed == ["fresh-tab"]


def test_no_retry_when_budget_cannot_cover_it(monkeypatch):
    _OnceIdle.instances = []
    _patch_discovery(monkeypatch)
    _patch_fresh_tab(monkeypatch)
    monkeypatch.setattr(gs, "_Interceptor", _OnceIdle._mint)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)
    # Default backoff (7.5s) + settle exceeds this budget -> the retry would
    # overrun the collector's deadline and must be skipped.
    result = gs.fetch_gemini_budget_usage(timeout=5.0, budget_seconds=5.0)
    assert result is None
    assert len(_OnceIdle.instances) == 1


def test_retry_when_budget_is_none_runs_unbounded(monkeypatch):
    # CLI/`/usage` callers pass no budget: retry proceeds even though the
    # per-request timeout is small (their deadline is not our business).
    _OnceIdle.instances = []
    _patch_discovery(monkeypatch)
    _patch_fresh_tab(monkeypatch)
    monkeypatch.setattr(gs, "_Interceptor", _OnceIdle._mint)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(gs, "_RETRY_BACKOFF_SECONDS", 0.01)

    assert gs.fetch_gemini_budget_usage(timeout=1.0, budget_seconds=None) == (
        14.9896,
        250.0,
    )
    assert len(_OnceIdle.instances) == 2


def test_success_on_existing_tab_never_retries_or_closes(monkeypatch):
    _OnceIdle.instances = []
    _patch_discovery(monkeypatch)
    closed = _patch_fresh_tab(monkeypatch)

    class _FirstTryOk(_FakeInterceptor):
        instances: list = []

        def __init__(self, bodies):
            super().__init__(bodies)
            _FirstTryOk.instances.append(self)

    fake = _FirstTryOk([_IMPORTED_PROJECTS_BODY, _USAGE_LIMITS_BODY])
    monkeypatch.setattr(gs, "_Interceptor", lambda ws: fake)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)

    assert gs.fetch_gemini_budget_usage(timeout=5.0, budget_seconds=60.0) == (
        14.9896,
        250.0,
    )
    # One attempt only; nothing opened, nothing to close.
    assert len(_FirstTryOk.instances) == 1
    assert closed == []


# --- interleaved-event survival ----------------------------------------------
#
# Production incident 2026-08-23 (second defect, same feature): _Interceptor.call
# read frames until it found ITS reply and threw everything else away. Any
# Fetch.requestPaused arriving inside a command round-trip was therefore lost --
# and a lost response-stage pause is never continued, so that request hangs
# forever and stalls the page lazy chain. Measured on the live apikey page the
# usage RPC lands ~10s in, well after a dozen other pauses have each cost one or
# two round-trips, so the event most likely to be swallowed is the only one we
# actually need. These tests pin that events survive a round-trip.


def _paused_frame(request_id, tail):
    import json as _json

    return _json.dumps(
        {
            "method": "Fetch.requestPaused",
            "params": {
                "requestId": request_id,
                "request": {"url": f"https://x/{tail}"},
            },
        }
    )


class _ScriptedWS:
    """Serves replies AND injects events while a reply is still in flight."""

    def __init__(self):
        self._out = []
        self.sent = []

    def send(self, raw):
        import json as _json

        msg = _json.loads(raw)
        msg_id, method = msg["id"], msg["method"]
        params = msg.get("params") or {}
        self.sent.append(method)
        if method == "Page.navigate":
            self._out.append(_paused_frame("req-1", "ListImportedProjects"))
            self._out.append(_json.dumps({"id": msg_id, "result": {}}))
        elif method == "Fetch.getResponseBody":
            if params.get("requestId") == "req-1":
                # The REAL usage pause lands while this reply is in flight.
                self._out.append(
                    _paused_frame("req-2", "BatchGetProjectUsageLimits")
                )
                body = _IMPORTED_PROJECTS_BODY
            else:
                body = _USAGE_LIMITS_BODY
            self._out.append(
                _json.dumps(
                    {"id": msg_id, "result": {"body": body, "base64Encoded": False}}
                )
            )
        else:
            self._out.append(_json.dumps({"id": msg_id, "result": {}}))

    def settimeout(self, seconds):
        pass

    def recv(self):
        if not self._out:
            raise RuntimeError("scripted socket exhausted")
        return self._out.pop(0)

    def close(self):
        pass


def test_call_queues_events_arriving_mid_round_trip():
    ws = _ScriptedWS()
    ws._out = [
        _paused_frame("swallowed", "BatchGetProjectUsageLimits"),
        '{"id": 1, "result": {"ok": true}}',
    ]
    interceptor = gs._Interceptor(ws)
    assert interceptor.call("Page.enable", {}, timeout=5.0) == {"ok": True}
    # The event arrived first; it must still be retrievable afterwards.
    event = interceptor.recv_event(0.2)
    assert event is not None, "interleaved Fetch.requestPaused was dropped"
    assert event["params"]["requestId"] == "swallowed"


def test_queued_events_drain_before_the_socket_is_read():
    ws = _ScriptedWS()
    ws._out = [
        _paused_frame("first", "ListImportedProjects"),
        _paused_frame("second", "BatchGetProjectUsageLimits"),
        '{"id": 1, "result": {}}',
    ]
    interceptor = gs._Interceptor(ws)
    interceptor.call("Page.enable", {}, timeout=5.0)
    assert interceptor.recv_event(0.2)["params"]["requestId"] == "first"
    assert interceptor.recv_event(0.2)["params"]["requestId"] == "second"


def test_usage_rpc_paused_during_a_round_trip_still_yields_limits(monkeypatch):
    """End-to-end through the REAL _Interceptor over a scripted socket."""
    _patch_discovery(monkeypatch)
    monkeypatch.setattr(
        "websocket.create_connection", lambda *a, **k: _ScriptedWS()
    )
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.5)
    # Budget deliberately too small to cover a fresh-tab retry, so this asserts
    # the FIRST attempt succeeds rather than being rescued by the retry.
    assert gs.fetch_gemini_budget_usage(timeout=5.0, budget_seconds=5.0) == (
        14.9896,
        250.0,
    )


# --- frozen-tab rejection ----------------------------------------------------
#
# Distinct from the wedged tab above, and diagnosed on grok_session 2026-08-25.
# A WEDGED tab runs JS and merely never completes the RPC chain, so only the
# full _SETTLE_SECONDS window reveals it. A FROZEN tab (Chrome background-tab
# freezing) executes nothing -- yet it still appears in /json/list with a valid
# webSocketDebuggerUrl, and the websocket still CONNECTS, because that handshake
# is browser-level rather than renderer-level. Only an evaluation tells, and it
# HANGS to the timeout rather than erroring. Left undetected it costs a whole
# 30s settle window, which then puts the fresh-tab retry over its
# backoff+settle budget gate and turns a recoverable state into "unavailable".


def test_liveness_probe_is_false_when_evaluate_never_answers(monkeypatch):
    class _Hanging:
        def send(self, raw):
            return None

        def recv(self):
            raise TimeoutError("Connection timed out")

        def close(self):
            return None

    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _Hanging())
    assert gs._target_is_responsive("ws://frozen") is False


def test_liveness_probe_is_true_when_evaluate_answers(monkeypatch):
    class _Answering:
        def __init__(self):
            self.sent = None

        def send(self, raw):
            self.sent = json.loads(raw)

        def recv(self):
            return json.dumps({"id": 1, "result": {"result": {"value": 1}}})

        def close(self):
            return None

    live = _Answering()
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: live)

    assert gs._target_is_responsive("ws://live") is True
    # It must be a real evaluation -- a mere connection proves nothing.
    assert live.sent["method"] == "Runtime.evaluate"


def test_liveness_probe_skips_interleaved_events_before_the_reply(monkeypatch):
    frames = [
        json.dumps({"method": "Page.frameNavigated", "params": {}}),
        json.dumps({"id": 1, "result": {"result": {"value": 1}}}),
    ]

    class _Chatty:
        def send(self, raw):
            return None

        def recv(self):
            return frames.pop(0)

        def close(self):
            return None

    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _Chatty())
    assert gs._target_is_responsive("ws://chatty") is True


def test_frozen_tab_is_skipped_so_a_fresh_one_is_opened(monkeypatch):
    """The regression: a frozen tab must not be handed to _attempt."""
    monkeypatch.setattr(
        gs, "_find_aistudio_targets",
        lambda http: [("frozen-id", "ws://frozen"), ("live-id", "ws://live")],
    )
    monkeypatch.setattr(
        gs, "_target_is_responsive",
        lambda ws, timeout=None: ws == "ws://live",
    )

    assert gs._live_aistudio_target("http://127.0.0.1:9222") == "ws://live"


def test_all_tabs_frozen_reads_as_no_tab(monkeypatch):
    """None routes the caller into the open-a-fresh-tab branch it already has."""
    monkeypatch.setattr(
        gs, "_find_aistudio_targets", lambda http: [("frozen", "ws://frozen")]
    )
    monkeypatch.setattr(
        gs, "_target_is_responsive", lambda ws, timeout=None: False
    )

    assert gs._live_aistudio_target("http://127.0.0.1:9222") is None


def test_a_frozen_tab_recovers_on_a_fresh_tab_within_a_collector_budget(monkeypatch):
    """End to end, and the point of the whole change.

    budget_seconds=20 is representative of the collector's per-provider fair
    share. The old path burned _SETTLE_SECONDS on the frozen tab and then
    refused the retry because 20 < backoff + settle; rejecting it up front
    means the fresh tab is reached and the snapshot is produced.
    """
    _OnceIdle.instances = []
    monkeypatch.setattr(
        gs, "discover_local_cdp_url", lambda port, timeout=None: "http://127.0.0.1:9222"
    )
    monkeypatch.setattr(
        gs, "_find_aistudio_targets", lambda http: [("frozen", "ws://frozen")]
    )
    monkeypatch.setattr(
        gs, "_target_is_responsive", lambda ws, timeout=None: False
    )
    closed = _patch_fresh_tab(monkeypatch)
    # No tab is "existing" any more, so the FIRST interceptor is the fresh tab's
    # and must succeed -- _OnceIdle idles only its first instance.
    _OnceIdle.instances = [object()]
    monkeypatch.setattr(gs, "_Interceptor", _OnceIdle._mint)
    monkeypatch.setattr("websocket.create_connection", lambda *a, **k: _DummyWS())
    monkeypatch.setattr(gs, "_SETTLE_SECONDS", 0.05)

    result = gs.fetch_gemini_budget_usage(timeout=5.0, budget_seconds=20.0)

    assert result == (14.9896, 250.0)
    assert closed == ["fresh-tab"]


def test_find_targets_returns_ids_and_skips_non_aistudio_pages():
    payload = [
        {"type": "page", "url": "https://grok.com/", "id": "a",
         "webSocketDebuggerUrl": "ws://a"},
        {"type": "page", "url": "https://aistudio.google.com/apikey", "id": "b",
         "webSocketDebuggerUrl": "ws://b"},
        # No id -> cannot be closed if we ever adopted it, so not returned.
        {"type": "page", "url": "https://aistudio.google.com/", "id": "",
         "webSocketDebuggerUrl": "ws://c"},
        {"type": "iframe", "url": "https://aistudio.google.com/", "id": "d",
         "webSocketDebuggerUrl": "ws://d"},
    ]

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
        found = gs._find_aistudio_targets("http://127.0.0.1:9222")
    finally:
        urllib.request.urlopen = orig

    assert found == [("b", "ws://b")]
