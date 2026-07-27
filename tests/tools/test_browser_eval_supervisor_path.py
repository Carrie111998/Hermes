"""Unit tests for the supervisor-WS fast path in browser_console / _browser_eval.

These exercise the dispatch logic in ``tools.browser_tool._browser_eval`` and
the response shaping in ``CDPSupervisor.evaluate_runtime`` using mocks — no
real browser, no real WebSocket.  Real-CDP coverage lives in
``tests/tools/test_browser_supervisor.py`` (gated on Chrome being installed).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fast-path dispatch: tools.browser_tool._browser_eval
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_camofox(monkeypatch):
    """Force the non-camofox path so our supervisor branch is reached."""
    import tools.browser_tool as bt

    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(bt, "_last_session_key", lambda task_id: "test-task")


def _patch_supervisor(monkeypatch, supervisor):
    """Wire SUPERVISOR_REGISTRY.get to return ``supervisor`` for any task_id."""
    import tools.browser_supervisor as bs

    registry = MagicMock()
    registry.get.return_value = supervisor
    monkeypatch.setattr(bs, "SUPERVISOR_REGISTRY", registry)
    return registry


class TestBrowserEvalSupervisorPath:
    """The supervisor fast path replaces the agent-browser subprocess hop."""

    def test_primitive_result_routes_through_supervisor(self, monkeypatch):
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": 42,
            "result_type": "number",
        }
        _patch_supervisor(monkeypatch, sup)
        # If the subprocess path is hit we want a loud failure.
        monkeypatch.setattr(
            bt, "_run_browser_command",
            lambda *a, **kw: pytest.fail("subprocess path must not run when supervisor is healthy"),
        )

        out = json.loads(bt._browser_eval("1 + 41"))
        assert out["success"] is True
        assert out["result"] == 42
        assert out["method"] == "cdp_supervisor"
        sup.evaluate_runtime.assert_called_once_with("1 + 41")

    def test_json_string_result_is_parsed(self, monkeypatch):
        """Match agent-browser semantics: JSON-string results get parsed."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": '{"a": 1, "b": [2, 3]}',
            "result_type": "string",
        }
        _patch_supervisor(monkeypatch, sup)
        monkeypatch.setattr(
            bt, "_run_browser_command",
            lambda *a, **kw: pytest.fail("subprocess path must not run"),
        )

        out = json.loads(bt._browser_eval('JSON.stringify({a:1,b:[2,3]})'))
        assert out["success"] is True
        assert out["result"] == {"a": 1, "b": [2, 3]}
        # result_type reflects the parsed Python type, not the raw JS type.
        assert out["result_type"] == "dict"

    def test_non_json_string_result_kept_as_string(self, monkeypatch):
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": True,
            "result": "hello world",
            "result_type": "string",
        }
        _patch_supervisor(monkeypatch, sup)
        monkeypatch.setattr(bt, "_run_browser_command", lambda *a, **kw: pytest.fail("nope"))

        out = json.loads(bt._browser_eval('"hello world"'))
        assert out["result"] == "hello world"
        assert out["result_type"] == "str"

    def test_js_exception_surfaces_without_subprocess_fallthrough(self, monkeypatch):
        """A JS-side error must NOT trigger a (slow + redundant) subprocess retry."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "Uncaught ReferenceError: foo is not defined",
        }
        _patch_supervisor(monkeypatch, sup)
        called = {"subprocess": False}

        def _fake_subprocess(*a, **kw):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "should-not-be-used"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("foo.bar"))
        assert out["success"] is False
        assert "ReferenceError" in out["error"]
        assert called["subprocess"] is False, \
            "JS exception should be surfaced, not retried via subprocess"

    def test_supervisor_loop_down_falls_through_to_subprocess(self, monkeypatch):
        """When the supervisor itself is unavailable, fall back to the subprocess."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "supervisor loop is not running",
        }
        _patch_supervisor(monkeypatch, sup)

        called = {"subprocess": False}

        def _fake_subprocess(task_id, cmd, args):
            called["subprocess"] = True
            assert cmd == "eval"
            return {"success": True, "data": {"result": "fallback-result"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("anything"))
        assert called["subprocess"] is True
        assert out["success"] is True
        assert out["result"] == "fallback-result"
        # Subprocess path doesn't tag the response with method=cdp_supervisor.
        assert out.get("method") != "cdp_supervisor"

    def test_no_active_supervisor_falls_through_to_subprocess(self, monkeypatch):
        """When SUPERVISOR_REGISTRY.get returns None, subprocess path runs."""
        import tools.browser_tool as bt

        _patch_supervisor(monkeypatch, None)
        called = {"subprocess": False}

        def _fake_subprocess(task_id, cmd, args):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "agent-browser-result"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("1+1"))
        assert called["subprocess"] is True
        assert out["success"] is True
        assert out.get("method") != "cdp_supervisor"

    def test_supervisor_no_session_falls_through(self, monkeypatch):
        """A supervisor without an attached page session must fall through cleanly."""
        import tools.browser_tool as bt

        sup = MagicMock()
        sup.evaluate_runtime.return_value = {
            "ok": False,
            "error": "supervisor has no attached page session",
        }
        _patch_supervisor(monkeypatch, sup)
        called = {"subprocess": False}

        def _fake_subprocess(*a, **kw):
            called["subprocess"] = True
            return {"success": True, "data": {"result": "fallback"}}

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)
        json.loads(bt._browser_eval("1+1"))
        assert called["subprocess"] is True

    def test_subprocess_reference_chain_error_becomes_guidance(self, monkeypatch):
        """The CLI subprocess can't retry with returnByValue=False, so the
        cryptic 'Object reference chain is too long' CDP error must be turned
        into actionable guidance instead of surfaced raw."""
        import tools.browser_tool as bt

        # No supervisor → subprocess path runs.
        _patch_supervisor(monkeypatch, None)

        def _fake_subprocess(task_id, cmd, args):
            assert cmd == "eval"
            return {
                "success": False,
                "error": "Runtime.evaluate failed: Object reference chain is too long",
            }

        monkeypatch.setattr(bt, "_run_browser_command", _fake_subprocess)

        out = json.loads(bt._browser_eval("document.body"))
        assert out["success"] is False
        # Raw protocol error must NOT leak through.
        assert "reference chain" not in out["error"].lower()
        # Actionable guidance instead.
        assert "primitive" in out["error"].lower()
        assert "DOM node" in out["error"] or "dom node" in out["error"].lower()


# ---------------------------------------------------------------------------
# Response shaping: CDPSupervisor.evaluate_runtime
# ---------------------------------------------------------------------------


def _make_supervisor_with_cdp(cdp_response):
    """Build a CDPSupervisor instance that mocks ``_cdp`` to return ``cdp_response``.

    Bypasses ``__init__`` entirely so we don't need a real WS connection.  We
    set just the state ``evaluate_runtime`` reads.
    """
    import asyncio
    import threading

    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup._state_lock = threading.Lock()
    sup._active = True
    sup._page_session_id = "test-session-id"
    sup._page_target_id = "test-target-id"
    sup._frames = {}

    # Build a real running event loop on a background thread so
    # asyncio.run_coroutine_threadsafe has somewhere to dispatch.
    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        return cdp_response

    sup._cdp = _fake_cdp  # type: ignore[method-assign]
    sup._loop = loop
    sup._thread = thread
    return sup


def _stop_supervisor(sup):
    sup._loop.call_soon_threadsafe(sup._loop.stop)
    sup._thread.join(timeout=2)


class TestEvaluateRuntimeResponseShaping:
    """CDPSupervisor.evaluate_runtime decodes the Runtime.evaluate response correctly."""

    def test_primitive_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {"result": {"type": "number", "value": 42}},
        })
        try:
            out = sup.evaluate_runtime("1 + 41")
            assert out == {"ok": True, "result": 42, "result_type": "number"}
        finally:
            _stop_supervisor(sup)

    def test_object_value_returned_by_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {
                    "type": "object",
                    "value": {"foo": "bar", "n": 7},
                }
            },
        })
        try:
            out = sup.evaluate_runtime('({foo:"bar", n:7})')
            assert out["ok"] is True
            assert out["result"] == {"foo": "bar", "n": 7}
            assert out["result_type"] == "object"
        finally:
            _stop_supervisor(sup)

    def test_undefined_value(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {"result": {"type": "undefined"}},
        })
        try:
            out = sup.evaluate_runtime("undefined")
            assert out == {"ok": True, "result": None, "result_type": "undefined"}
        finally:
            _stop_supervisor(sup)

    def test_dom_node_returns_description(self):
        """Non-serializable values (DOM nodes, functions) come back as description strings."""
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {
                    "type": "object",
                    "subtype": "node",
                    "description": "div#main.app",
                    # No 'value' key — returnByValue couldn't serialize it.
                }
            },
        })
        try:
            out = sup.evaluate_runtime("document.querySelector('#main')")
            assert out["ok"] is True
            assert out["result"] == "div#main.app"
            assert out["result_type"] == "object"
        finally:
            _stop_supervisor(sup)

    def test_js_exception_returns_error(self):
        sup = _make_supervisor_with_cdp({
            "id": 1,
            "result": {
                "result": {"type": "undefined"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {
                        "description": "ReferenceError: foo is not defined",
                    },
                },
            },
        })
        try:
            out = sup.evaluate_runtime("foo.bar")
            assert out["ok"] is False
            assert "ReferenceError" in out["error"]
        finally:
            _stop_supervisor(sup)

    def test_inactive_supervisor_returns_error_without_dispatch(self):
        """Inactive supervisor short-circuits before even touching the loop."""
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = object.__new__(CDPSupervisor)
        sup._state_lock = threading.Lock()
        sup._active = False  # ← key
        sup._page_session_id = None
        sup._loop = None

        out = sup.evaluate_runtime("1+1")
        assert out["ok"] is False
        # Either "loop is not running" or "is not active" is acceptable —
        # both are caught by the supervisor-side error branch in _browser_eval.
        assert "supervisor" in out["error"].lower()

    def test_no_session_attached_returns_error(self):
        import asyncio
        import threading
        from tools.browser_supervisor import CDPSupervisor

        sup = object.__new__(CDPSupervisor)
        sup._state_lock = threading.Lock()
        sup._active = True
        sup._page_session_id = None  # ← attach hasn't happened yet

        loop = asyncio.new_event_loop()
        thread = threading.Thread(
            target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()),
            daemon=True,
        )
        thread.start()
        sup._loop = loop
        try:
            out = sup.evaluate_runtime("1+1")
            assert out["ok"] is False
            assert "session" in out["error"].lower()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)


def _make_supervisor_with_cdp_fn(cdp_fn):
    """Like ``_make_supervisor_with_cdp`` but lets the test supply a coroutine
    function as ``_cdp`` so behaviour can vary by params (e.g. returnByValue).
    """
    import asyncio
    import threading

    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup._state_lock = threading.Lock()
    sup._active = True
    sup._page_session_id = "test-session-id"
    sup._page_target_id = "test-target-id"
    sup._frames = {}

    loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    sup._cdp = cdp_fn  # type: ignore[method-assign]
    sup._loop = loop
    sup._thread = thread
    return sup


def test_capture_ax_tree_retargets_unique_exact_page():
    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "new-tab",
                            "url": "chrome://newtab/",
                        },
                        {
                            "type": "page",
                            "targetId": "facebook",
                            "url": "https://www.facebook.com/marketplace/you/selling",
                        },
                    ]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "facebook-session"}}
        if method == "Accessibility.getFullAXTree":
            return {"result": {"nodes": [{"backendDOMNodeId": 42}]}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(session_id):
        calls.append(("configure", None, session_id))

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url(
            "https://www.facebook.com/marketplace/you/selling"
        )
        assert result["ok"] is True
        assert result["target_id"] == "facebook"
        assert result["session_id"] == "facebook-session"
        assert result["result"]["nodes"][0]["backendDOMNodeId"] == 42
        assert sup._page_target_id == "facebook"
        assert sup._page_session_id == "facebook-session"
        assert [call[0] for call in calls] == [
            "Target.getTargets",
            "Target.attachToTarget",
            "configure",
            "Accessibility.getFullAXTree",
            "Target.detachFromTarget",
        ]
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_fails_closed_when_target_is_ambiguous():
    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        assert method == "Target.getTargets"
        return {
            "result": {
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "facebook-1",
                        "url": "https://www.facebook.com/groups/123",
                    },
                    {
                        "type": "page",
                        "targetId": "facebook-2",
                        "url": "https://www.facebook.com/groups/123",
                    },
                ]
            }
        }

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    try:
        result = sup.capture_ax_tree_for_url(
            "https://www.facebook.com/groups/123"
        )
        assert result["ok"] is False
        assert "found 2" in result["error"]
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_reattaches_even_when_target_id_is_cached():
    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append(method)
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [{
                        "type": "page",
                        "targetId": "test-target-id",
                        "url": "https://example.com/current",
                    }]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "fresh-session"}}
        if method == "Accessibility.getFullAXTree":
            return {"result": {"nodes": []}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def fake_configure(_session_id):
        calls.append("configure")

    sup._configure_page_session = fake_configure
    try:
        result = sup.capture_ax_tree_for_url("https://example.com/current")
        assert result["ok"] is True
        assert sup._page_session_id == "fresh-session"
        assert calls == [
            "Target.getTargets",
            "Target.attachToTarget",
            "configure",
            "Accessibility.getFullAXTree",
            "Target.detachFromTarget",
        ]
    finally:
        _stop_supervisor(sup)


def test_capture_ax_tree_timeout_cleans_up_without_late_adoption():
    import asyncio
    import time

    calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        calls.append((method, params))
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [{
                        "type": "page",
                        "targetId": "fresh-target",
                        "url": "https://example.com/current",
                    }]
                }
            }
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "fresh-session"}}
        if method == "Target.detachFromTarget":
            return {"result": {}}
        raise AssertionError(f"unexpected CDP call: {method}")

    sup = _make_supervisor_with_cdp_fn(fake_cdp)

    async def slow_configure(_session_id):
        await asyncio.sleep(1)

    sup._configure_page_session = slow_configure
    try:
        result = sup.capture_ax_tree_for_url(
            "https://example.com/current",
            timeout=0.01,
        )
        time.sleep(0.05)
        assert result["ok"] is False
        assert sup._page_target_id == "test-target-id"
        assert sup._page_session_id == "test-session-id"
        assert (
            "Target.detachFromTarget",
            {"sessionId": "fresh-session"},
        ) in calls
    finally:
        _stop_supervisor(sup)


def test_queued_ax_capture_does_not_dispatch_after_caller_timeout():
    import threading
    import time

    cdp_calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        cdp_calls.append(method)
        return {"result": {"targetInfos": []}}

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    blocker_started = threading.Event()

    def block_loop():
        blocker_started.set()
        time.sleep(1.2)

    try:
        sup._loop.call_soon_threadsafe(block_loop)
        assert blocker_started.wait(timeout=1)
        result = sup.capture_ax_tree_for_url(
            "https://example.com/current",
            timeout=0.01,
        )
        time.sleep(0.3)
        assert result["ok"] is False
        assert cdp_calls == []
        assert sup._page_session_id == "test-session-id"
    finally:
        _stop_supervisor(sup)


def test_queued_captured_action_is_ambiguous_and_never_dispatches_late():
    import threading
    import time

    cdp_calls = []

    async def fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
        cdp_calls.append(method)
        return {"result": {}}

    sup = _make_supervisor_with_cdp_fn(fake_cdp)
    blocker_started = threading.Event()

    def block_loop():
        blocker_started.set()
        time.sleep(1.2)

    try:
        sup._loop.call_soon_threadsafe(block_loop)
        assert blocker_started.wait(timeout=1)
        result = sup.call_session_cdp(
            "captured-session",
            "Runtime.callFunctionOn",
            {"objectId": "node-1"},
            timeout=0.01,
        )
        time.sleep(0.3)
        assert result["ok"] is False
        assert result["dispatch_ambiguous"] is True
        assert cdp_calls == []
    finally:
        _stop_supervisor(sup)


class TestEvaluateRuntimeDomNodeCrashRetry:
    """returnByValue=True on a DOM node fails CDP serialization with 'Object
    reference chain is too long'.  evaluate_runtime must retry with
    returnByValue=False and return the node's description instead of crashing.
    """

    def test_reference_chain_crash_retries_without_by_value(self):
        calls = []

        async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
            by_value = (params or {}).get("returnByValue")
            calls.append(by_value)
            if by_value:
                # Mirror _read_loop turning a top-level CDP error into a RuntimeError.
                raise RuntimeError(
                    "CDP error on id=7: {'code': -32000, "
                    "'message': 'Object reference chain is too long'}"
                )
            # returnByValue=False: Chrome returns the node's description, no value.
            return {
                "id": 8,
                "result": {
                    "result": {
                        "type": "object",
                        "subtype": "node",
                        "description": "body",
                    }
                },
            }

        sup = _make_supervisor_with_cdp_fn(_fake_cdp)
        try:
            out = sup.evaluate_runtime("document.body")
            assert out["ok"] is True
            assert out["result"] == "body"
            assert out["result_type"] == "object"
            # First call by_value=True (crashed), retried with by_value=False.
            assert calls == [True, False]
        finally:
            _stop_supervisor(sup)

    def test_unrelated_error_does_not_retry(self):
        calls = []

        async def _fake_cdp(method, params=None, *, session_id=None, timeout=10.0):
            calls.append((params or {}).get("returnByValue"))
            raise RuntimeError("CDP error on id=3: {'message': 'Target closed'}")

        sup = _make_supervisor_with_cdp_fn(_fake_cdp)
        try:
            out = sup.evaluate_runtime("document.body")
            assert out["ok"] is False
            assert "Target closed" in out["error"]
            # No retry for unrelated failures — exactly one call.
            assert calls == [True]
        finally:
            _stop_supervisor(sup)
