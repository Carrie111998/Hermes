"""Dashboard API and PTY integration for guided one-shot launches."""

from __future__ import annotations

from urllib.parse import urlencode, urlparse, parse_qs

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_cli import guided_launch
from hermes_cli.guided_launch import _reset_for_tests, mint_guided_launch
from hermes_cli.pty_bridge import PtyUnavailableError


@pytest.fixture(autouse=True)
def _reset_launches():
    _reset_for_tests()
    yield
    _reset_for_tests()


@pytest.fixture
def server(monkeypatch):
    import hermes_cli.web_server as web_server

    monkeypatch.setattr(web_server, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    web_server.app.state.auth_required = False
    return web_server, TestClient(web_server.app)


def _body(**overrides):
    values = {
        "profile": "default",
        "conversation_id": "Bot Chat",
        "session_id": "20260822_120000_deadbeef",
        "board": "mission-control",
        "task_id": "t_afd09696",
        "brief": "Open the form and stop before Submit.",
        "lease_id": "lease-123",
        "approval_surface": "http://100.65.87.91:9119/chat",
        "approval_decision": "approved",
        "approval_expires_at": 1_000_300,
        "lease_expires_at": 1_000_300,
        "expires_at": 1_000_300,
    }
    values.update(overrides)
    return values


def _mint(monkeypatch):
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)
    token, claim = mint_guided_launch(**_body())
    query = {
        "guided": token,
        "profile": claim["profile"],
        "conversation": claim["conversation_id"],
        "resume": claim["session_id"],
        "board": claim["board"],
        "task": claim["task_id"],
        "lease": claim["lease_id"],
        "brief_sha256": claim["brief_sha256"],
    }
    return token, claim, query


def test_mint_endpoint_requires_dashboard_auth(server, monkeypatch):
    web_server, client = server
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)

    response = client.post("/api/chat/guided-launch", json=_body())

    assert response.status_code == 401
    assert guided_launch._active_count_for_tests() == 0


def test_mint_endpoint_returns_bound_chat_href(server, monkeypatch):
    web_server, client = server
    monkeypatch.setattr(guided_launch.time, "time", lambda: 1_000_000)

    response = client.post(
        "/api/chat/guided-launch",
        headers={"X-Hermes-Session-Token": web_server._SESSION_TOKEN},
        json=_body(),
    )

    assert response.status_code == 200
    payload = response.json()
    query = parse_qs(urlparse(payload["href"]).query)
    assert query["profile"] == ["default"]
    assert query["conversation"] == ["Bot Chat"]
    assert query["resume"] == ["20260822_120000_deadbeef"]
    assert query["board"] == ["mission-control"]
    assert query["task"] == ["t_afd09696"]
    assert query["lease"] == ["lease-123"]
    assert len(query["brief_sha256"][0]) == 64
    assert query["guided"] == [payload["launch_token"]]
    assert payload["expires_at"] == 1_000_300


def test_valid_pty_launch_injects_exact_prompt_once_and_replay_spawns_nothing(server, monkeypatch):
    web_server, client = server
    _, claim, query = _mint(monkeypatch)
    query.update({"token": web_server._SESSION_TOKEN, "channel": "guided-test"})
    resolver_calls = []
    spawn_envs = []

    async def fake_resolver(**kwargs):
        resolver_calls.append(kwargs)
        return ["/bin/cat"], None, {"BASE": "kept"}

    def fake_spawn(cls, argv, *, cwd=None, env=None):
        spawn_envs.append(dict(env or {}))
        raise PtyUnavailableError("stop after env capture")

    monkeypatch.setattr(web_server, "_resolve_chat_argv_async", fake_resolver)
    monkeypatch.setattr(web_server.PtyBridge, "spawn", classmethod(fake_spawn))
    path = "/api/pty?" + urlencode(query)

    with client.websocket_connect(path) as ws:
        notice = ws.receive_text()
        assert "Chat unavailable" in notice
    assert resolver_calls == [
        {
            "resume": claim["session_id"],
            "sidecar_url": resolver_calls[0]["sidecar_url"],
            "profile": "default",
            "active_session_file": resolver_calls[0]["active_session_file"],
        }
    ]
    assert spawn_envs[0]["BASE"] == "kept"
    prompt = spawn_envs[0]["HERMES_TUI_QUERY"]
    assert claim["brief"] in prompt
    assert "Task: t_afd09696" in prompt
    assert "Lease: lease-123" in prompt

    with client.websocket_connect(path) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4411
    assert len(resolver_calls) == 1
    assert len(spawn_envs) == 1


def test_tampered_binding_is_consumed_before_resolver_or_spawn(server, monkeypatch):
    web_server, client = server
    _, _, query = _mint(monkeypatch)
    query.update(
        {
            "token": web_server._SESSION_TOKEN,
            "channel": "guided-tamper",
            "task": "t_tampered",
        }
    )
    calls = []

    async def never_resolve(**kwargs):
        calls.append(kwargs)
        raise AssertionError("resolver must not run")

    monkeypatch.setattr(web_server, "_resolve_chat_argv_async", never_resolve)
    path = "/api/pty?" + urlencode(query)

    with client.websocket_connect(path) as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 4411
    assert calls == []


def test_generic_pty_url_never_gets_a_startup_query(server, monkeypatch):
    web_server, client = server
    captured = []

    async def fake_resolver(**kwargs):
        return ["/bin/cat"], None, {"BASE": "kept"}

    def fake_spawn(cls, argv, *, cwd=None, env=None):
        captured.append(dict(env or {}))
        raise PtyUnavailableError("stop after env capture")

    monkeypatch.setattr(web_server, "_resolve_chat_argv_async", fake_resolver)
    monkeypatch.setattr(web_server.PtyBridge, "spawn", classmethod(fake_spawn))
    path = "/api/pty?" + urlencode(
        {
            "token": web_server._SESSION_TOKEN,
            "channel": "ordinary-chat",
            "profile": "default",
        }
    )

    with client.websocket_connect(path) as ws:
        assert "Chat unavailable" in ws.receive_text()
    assert "HERMES_TUI_QUERY" not in captured[0]
