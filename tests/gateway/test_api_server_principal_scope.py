"""API-server principal scope propagation tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import (
    APIServerAdapter,
    cors_middleware,
    security_headers_middleware,
)


def _make_adapter(api_key: str = "") -> APIServerAdapter:
    extra = {"key": api_key} if api_key else {}
    return APIServerAdapter(PlatformConfig(enabled=True, extra=extra))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    return app


def _principal_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer sk-secret",
        "X-Hermes-Tenant-Id": "tenant-1",
        "X-Hermes-Workspace-Id": "workspace-1",
        "X-Hermes-Project-Id": "project-1",
        "X-Hermes-User-Id": "user-1",
        "X-Hermes-Roles": "member,admin",
        "X-Hermes-Sandbox-Id": "sandbox-1",
    }


@pytest.mark.asyncio
async def test_chat_completions_passes_principal_scope_to_run_agent():
    adapter = _make_adapter(api_key="sk-secret")
    app = _create_app(adapter)

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers=_principal_headers(),
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

    assert resp.status == 200
    assert mock_run.await_args.kwargs["principal_scope"] == {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "roles": ("member", "admin"),
        "sandbox_id": "sandbox-1",
    }


@pytest.mark.asyncio
async def test_principal_scope_headers_require_api_key_configuration():
    adapter = _make_adapter()
    app = _create_app(adapter)
    headers = {"X-Hermes-User-Id": "user-1"}

    with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )
            body = await resp.json()

    assert resp.status == 403
    assert mock_run.await_count == 0
    assert "API key" in body["error"]["message"]


@pytest.mark.asyncio
async def test_runs_binds_principal_scope_during_agent_execution():
    adapter = _make_adapter(api_key="sk-secret")
    app = _create_app(adapter)
    observed: dict[str, object] = {}

    def _observe_scope(user_message, conversation_history, task_id):
        from agent.ultra_security import get_current_principal, get_current_sandbox_lease

        principal = get_current_principal()
        lease = get_current_sandbox_lease()
        observed.update(
            {
                "task_id": task_id,
                "tenant_id": principal.tenant_id if principal else "",
                "workspace_id": principal.workspace_id if principal else "",
                "project_id": principal.project_id if principal else "",
                "user_id": principal.user_id if principal else "",
                "roles": principal.roles if principal else (),
                "sandbox_id": lease.sandbox_id if lease else "",
            }
        )
        return {"final_response": "done"}

    fake_agent = MagicMock()
    fake_agent.run_conversation.side_effect = _observe_scope
    fake_agent.session_prompt_tokens = 0
    fake_agent.session_completion_tokens = 0
    fake_agent.session_total_tokens = 0

    with patch.object(adapter, "_create_agent", return_value=fake_agent):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/runs",
                headers=_principal_headers(),
                json={"input": "hello", "session_id": "session-1"},
            )
            assert resp.status == 202
            data = await resp.json()

            for _ in range(50):
                status_resp = await cli.get(
                    f"/v1/runs/{data['run_id']}",
                    headers={"Authorization": "Bearer sk-secret"},
                )
                status = await status_resp.json()
                if status["status"] == "completed":
                    break

    assert observed == {
        "task_id": "session-1",
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "project_id": "project-1",
        "user_id": "user-1",
        "roles": ("member", "admin"),
        "sandbox_id": "sandbox-1",
    }
