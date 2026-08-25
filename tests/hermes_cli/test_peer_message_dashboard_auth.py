"""Dashboard-auth boundary for the hosted peer-message endpoint."""

from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from hermes_cli.dashboard_auth.middleware import gated_auth_middleware


def test_peer_message_uses_its_own_bearer_auth() -> None:
    """The OAuth gate must not consume the peer key before the route does."""
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "https",
        "server": ("agent.example", 443),
        "client": ("127.0.0.1", 12345),
        "path": "/api/v1/message",
        "raw_path": b"/api/v1/message",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer opaque-peer-key")],
        "app": type("App", (), {"state": type("State", (), {"auth_required": True})()})(),
    }
    request = Request(scope)
    reached_route = False

    async def call_next(_request: Request):
        nonlocal reached_route
        assert _request.headers.get("authorization") == "Bearer opaque-peer-key"
        reached_route = True
        return JSONResponse({"detail": "peer verifier owns this request"}, status_code=401)

    response = asyncio.run(gated_auth_middleware(request, call_next))

    assert reached_route is True
    assert response.status_code == 401