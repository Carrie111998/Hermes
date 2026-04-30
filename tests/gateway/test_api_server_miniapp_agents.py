from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware, security_headers_middleware
from gateway.platforms.miniapp_agents import MiniAppAgentRegistry


def _make_adapter(api_key: str = "sk-secret", registry: MiniAppAgentRegistry | None = None) -> APIServerAdapter:
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": api_key}))
    if registry is not None:
        adapter._miniapp_agent_registry = registry
    return adapter


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware, security_headers_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_get("/api/agents", adapter._handle_list_agents)
    app.router.add_post("/api/agents", adapter._handle_spawn_agent)
    app.router.add_get("/api/agents/{name}", adapter._handle_get_agent)
    app.router.add_post("/api/agents/{name}/message", adapter._handle_send_agent_message)
    app.router.add_delete("/api/agents/{name}", adapter._handle_delete_agent)
    return app


@pytest.mark.asyncio
async def test_spawn_agent_creates_registry_entry(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    with patch("gateway.platforms.miniapp_agents.process_registry.spawn_local") as spawn, \
         patch("gateway.platforms.miniapp_agents.process_registry.submit_stdin", return_value={"status": "ok"}) as send:
        spawn.return_value = SimpleNamespace(id="proc_abc123", pid=9911, started_at=1_700_000_000)
        agent = registry.spawn(prompt="Run the smoke test", name="smoke", mode="interactive", worktree=False)

    assert agent["name"] == "smoke"
    assert agent["session_id"] == "proc_abc123"
    assert agent["status"] == "running"
    send.assert_called_once_with("proc_abc123", "Run the smoke test")


def test_list_agents_returns_running_rows(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )

    with patch("gateway.platforms.miniapp_agents.process_registry.poll", return_value={"status": "running", "uptime_seconds": 42}):
        rows = registry.list_agents()

    assert rows[0]["name"] == "smoke"
    assert rows[0]["status"] == "running"
    assert rows[0]["uptime"] == 42


def test_send_message_appends_to_interactive_agent_stdin(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )

    with patch("gateway.platforms.miniapp_agents.process_registry.submit_stdin", return_value={"status": "ok"}) as send:
        registry.send_message("smoke", "continue with the refactor")

    send.assert_called_once_with("proc_abc123", "continue with the refactor")


def test_delete_agent_kills_background_process(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )

    with patch("gateway.platforms.miniapp_agents.process_registry.kill_process", return_value={"status": "killed"}) as kill:
        registry.delete("smoke")

    kill.assert_called_once_with("proc_abc123")
    assert registry.list_agents() == []


@pytest.mark.asyncio
async def test_api_list_agents_returns_registry_rows(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )
    adapter = _make_adapter(registry=registry)
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.miniapp_agents.process_registry.poll", return_value={"status": "running", "uptime_seconds": 42}):
            resp = await cli.get("/api/agents", headers={"Authorization": "Bearer sk-secret"})
            data = await resp.json()

    assert resp.status == 200
    assert data["agents"][0]["name"] == "smoke"


@pytest.mark.asyncio
async def test_api_spawn_agent_round_trip(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    adapter = _make_adapter(registry=registry)
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.miniapp_agents.process_registry.spawn_local") as spawn, \
             patch("gateway.platforms.miniapp_agents.process_registry.submit_stdin", return_value={"status": "ok"}):
            spawn.return_value = SimpleNamespace(id="proc_abc123", pid=9911, started_at=1_700_000_000)
            resp = await cli.post(
                "/api/agents",
                headers={"Authorization": "Bearer sk-secret"},
                json={"name": "smoke", "prompt": "Run the smoke test", "mode": "interactive", "worktree": False},
            )
            data = await resp.json()

    assert resp.status == 200
    assert data["name"] == "smoke"


@pytest.mark.asyncio
async def test_api_get_agent_includes_output(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )
    adapter = _make_adapter(registry=registry)
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.miniapp_agents.process_registry.poll", return_value={"status": "running", "uptime_seconds": 42}), \
             patch("gateway.platforms.miniapp_agents.process_registry.read_log", return_value={"output": "hello world"}):
            resp = await cli.get("/api/agents/smoke", headers={"Authorization": "Bearer sk-secret"})
            data = await resp.json()

    assert resp.status == 200
    assert data["output"] == "hello world"


@pytest.mark.asyncio
async def test_api_send_agent_message_calls_registry(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )
    adapter = _make_adapter(registry=registry)
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.miniapp_agents.process_registry.submit_stdin", return_value={"status": "ok"}) as send:
            resp = await cli.post(
                "/api/agents/smoke/message",
                headers={"Authorization": "Bearer sk-secret"},
                json={"message": "continue"},
            )
            data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is True
    send.assert_called_once_with("proc_abc123", "continue")


@pytest.mark.asyncio
async def test_api_delete_agent_kills_process(tmp_path):
    registry = MiniAppAgentRegistry(tmp_path / "miniapp_agents.json")
    registry._save(
        {
            "smoke": {
                "name": "smoke",
                "display_name": "smoke",
                "session_id": "proc_abc123",
                "status": "running",
                "mode": "interactive",
                "worktree": False,
                "model": "hermes-agent",
                "started_at": 1_700_000_000,
            }
        }
    )
    adapter = _make_adapter(registry=registry)
    app = _create_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        with patch("gateway.platforms.miniapp_agents.process_registry.kill_process", return_value={"status": "killed"}) as kill:
            resp = await cli.delete("/api/agents/smoke", headers={"Authorization": "Bearer sk-secret"})
            data = await resp.json()

    assert resp.status == 200
    assert data["ok"] is True
    kill.assert_called_once_with("proc_abc123")
