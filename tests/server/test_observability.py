"""Request correlation, error masking, and readiness checks."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from test_webui import make_client  # noqa: E402


def _client_with_failing_route():
    """WebUI off: its StaticFiles mount at "/" would shadow the test route."""
    app, _ = make_client(webui_enabled=False)

    @app.get("/boom")
    def boom():
        raise ValueError("secret internal detail 12345")

    return app, TestClient(app, raise_server_exceptions=False)


def test_ready_reports_database_state():
    _app, client = make_client()
    res = client.get("/ready")
    assert res.status_code == 200 and res.json() == {"status": "ready", "database": "ok"}


def test_ready_fails_closed_when_the_database_is_unreachable():
    app, client = make_client()

    def broken(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    app.state.db.one = broken  # simulate Postgres being down
    res = client.get("/ready")
    assert res.status_code == 503 and res.json()["database"] == "error"


def test_health_stays_shallow_and_does_not_touch_the_database():
    """/health is liveness only; /ready is the rollout gate."""
    app, client = make_client()

    def broken(*_args, **_kwargs):
        raise RuntimeError("connection refused")

    app.state.db.one = broken
    assert client.get("/health").status_code == 200


def test_unhandled_errors_are_masked_but_correlated(caplog):
    _app, client = _client_with_failing_route()
    with caplog.at_level(logging.ERROR):
        res = client.get("/boom")
    assert res.status_code == 500
    body = res.json()
    # the client learns nothing about internals ...
    assert body["detail"] == "Internal server error"
    assert "secret internal detail" not in res.text
    # ... but the operator can join the response to the log line
    assert body["request_id"] == res.headers["X-Request-ID"] != "-"
    assert "secret internal detail" in caplog.text


def test_request_id_is_echoed_and_sanitized():
    _app, client = make_client()
    assert client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    # header injection / log forging must not survive
    dirty = client.get("/health", headers={"X-Request-ID": "bad\tval;drop table"})
    assert dirty.headers["X-Request-ID"] == "badvaldroptable"
    # and an over-long value is bounded
    assert len(client.get("/health", headers={"X-Request-ID": "a" * 500}).headers["X-Request-ID"]) == 64


def test_request_id_is_generated_when_absent():
    _app, client = make_client()
    generated = client.get("/health").headers["X-Request-ID"]
    assert generated and generated != "-"
    assert client.get("/health").headers["X-Request-ID"] != generated


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and "caplog" not in value.__code__.co_varnames]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} observability checks passed (caplog tests need pytest)")
