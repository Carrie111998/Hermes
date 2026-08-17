"""Regression tests: bad int-shaped params must not crash the gateway.

Several session handlers turn a client param into an int with ``int(...)``.
A dict, list, or non-numeric string makes that call raise. These handlers run
inline on the gateway's stdin reader thread, and the entry loop does not catch
handler errors, so one bad frame kills the whole gateway process (the TUI goes
dark and has to respawn).

Every int-shaped param now goes through ``server._coerce_int``, which falls
back to a default instead of raising. ``terminal.resize`` is the one handler
that does not fall back: an explicit resize has no sensible default, so it
answers a 4000 error.
"""

import pytest

import tui_gateway.server as server


# --- the shared helper every site funnels through ---

BAD_INT_VALUES = [{}, [], None, "not-a-number", "\r\n", [1, 2], {"a": 1}]


@pytest.mark.parametrize("bad", BAD_INT_VALUES)
def test_coerce_int_bad_type_returns_default(bad):
    assert server._coerce_int(bad, 80) == 80


@pytest.mark.parametrize(
    "value,expected",
    [
        (120, 120),
        ("200", 200),
        (1.5, 1),   # int(1.5) is 1, floats coerce and never crash
        (True, 1),
    ],
)
def test_coerce_int_good_value_coerces(value, expected):
    assert server._coerce_int(value, 80) == expected


# --- handlers that used to crash on a bad value ---


@pytest.fixture(autouse=True)
def _no_background_builds(monkeypatch):
    # session.create arms a deferred agent build. Keep tests synchronous.
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *a, **k: None)


def _request(method, params):
    return {"jsonrpc": "2.0", "id": "r1", "method": method, "params": params}


@pytest.mark.parametrize("bad", [{}, [], None, "x"])
def test_session_create_bad_cols_returns_response_not_crash(bad):
    resp = server.handle_request(_request("session.create", {"cols": bad}))
    assert isinstance(resp, dict)
    # Either a created session (cols falls back to the default) or a clean
    # JSON-RPC error, never an exception escaping the handler.
    assert "error" in resp or "result" in resp


def test_session_create_bad_cols_falls_back_to_default():
    resp = server.handle_request(_request("session.create", {"cols": {}}))
    assert "error" not in resp


def test_terminal_resize_bad_cols_returns_4000():
    # terminal.resize looks the session up first, so a live session is needed.
    server._sessions["sess-resize-test"] = {
        "session_key": "sess-resize-test",
        "history": [],
    }
    try:
        resp = server.handle_request(
            _request("terminal.resize", {"session_id": "sess-resize-test", "cols": []})
        )
        assert isinstance(resp, dict)
        assert resp["error"]["code"] == 4000
    finally:
        server._sessions.pop("sess-resize-test", None)


@pytest.mark.parametrize("method", ["session.list", "spawn_tree.list"])
def test_list_handlers_bad_limit_returns_response_not_crash(method):
    resp = server.handle_request(_request(method, {"limit": {}}))
    assert isinstance(resp, dict)
    assert "error" in resp or "result" in resp
