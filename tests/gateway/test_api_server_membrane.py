"""Tests for /v1/runs session history hydration + membrane outbound send path."""

from __future__ import annotations

import asyncio
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
from gateway.platforms.base import SendResult


def _make_adapter(api_key: str = "sk-secret") -> APIServerAdapter:
    config = PlatformConfig(enabled=True, extra={"key": api_key} if api_key else {})
    return APIServerAdapter(config)


def _create_runs_app(adapter: APIServerAdapter) -> web.Application:
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/runs", adapter._handle_runs)
    app.router.add_get("/v1/runs/{run_id}", adapter._handle_get_run)
    app.router.add_get("/v1/membrane/outbound", adapter._handle_membrane_outbound_list)
    app.router.add_post("/v1/membrane/outbound/ack", adapter._handle_membrane_outbound_ack)
    return app


class TestRunsSessionHistoryHydration:
    @pytest.mark.asyncio
    async def test_session_id_loads_history_from_db(self):
        """Membrane path: body.session_id + empty history → SessionDB hydrate."""
        adapter = _make_adapter()
        db_history = [
            {"role": "user", "content": "ticket de caisse ..."},
            {"role": "assistant", "content": "total 351 €"},
        ]
        captured = {}

        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()

                def _run(user_message=None, conversation_history=None, task_id=None):
                    captured["user_message"] = user_message
                    captured["conversation_history"] = conversation_history
                    return {"final_response": "ok prix inclus"}

                mock_agent.run_conversation.side_effect = _run
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                with patch.object(
                    adapter,
                    "_conversation_history_for_session",
                    new_callable=AsyncMock,
                    return_value=db_history,
                ) as mock_load:
                    auth = {"Authorization": "Bearer sk-secret"}
                    resp = await cli.post(
                        "/v1/runs",
                        headers=auth,
                        json={
                            "input": "le prix inclut deja la multiplication",
                            "session_id": "telegram:dm:8078895371",
                        },
                    )
                    assert resp.status == 202
                    data = await resp.json()
                    run_id = data["run_id"]
                    for _ in range(40):
                        st = await (await cli.get(f"/v1/runs/{run_id}", headers=auth)).json()
                        if st.get("status") == "completed":
                            break
                        await asyncio.sleep(0.05)

                mock_load.assert_awaited()
                assert mock_load.await_args.args[0] == "telegram:dm:8078895371"

        assert captured["user_message"] == "le prix inclut deja la multiplication"
        assert captured["conversation_history"] == db_history

    @pytest.mark.asyncio
    async def test_explicit_history_skips_db_load(self):
        adapter = _make_adapter()
        body_history = [{"role": "user", "content": "from body"}]
        captured = {}
        app = _create_runs_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_create_agent") as mock_create:
                mock_agent = MagicMock()

                def _run(user_message=None, conversation_history=None, task_id=None):
                    captured["history"] = conversation_history
                    return {"final_response": "ok"}

                mock_agent.run_conversation.side_effect = _run
                mock_agent.session_prompt_tokens = 0
                mock_agent.session_completion_tokens = 0
                mock_agent.session_total_tokens = 0
                mock_create.return_value = mock_agent

                with patch.object(
                    adapter,
                    "_conversation_history_for_session",
                    new_callable=AsyncMock,
                ) as mock_load:
                    auth = {"Authorization": "Bearer sk-secret"}
                    resp = await cli.post(
                        "/v1/runs",
                        headers=auth,
                        json={
                            "input": "hi",
                            "session_id": "telegram:dm:1",
                            "conversation_history": body_history,
                        },
                    )
                    assert resp.status == 202
                    data = await resp.json()
                    for _ in range(40):
                        st = await (
                            await cli.get(f"/v1/runs/{data['run_id']}", headers=auth)
                        ).json()
                        if st.get("status") == "completed":
                            break
                        await asyncio.sleep(0.05)
                mock_load.assert_not_awaited()

        assert captured["history"] == body_history


class TestApiServerMembraneSend:
    @pytest.mark.asyncio
    async def test_send_enqueues_telegram_dm_target(self):
        adapter = _make_adapter()
        with patch(
            "gateway.membrane_outbound.enqueue", return_value="mo_abc"
        ) as mock_enq:
            result = await adapter.send(
                "telegram:dm:8078895371",
                "cron veille report",
            )
        assert isinstance(result, SendResult)
        assert result.success is True
        assert result.message_id == "mo_abc"
        mock_enq.assert_called_once()
        assert mock_enq.call_args.kwargs["chat_id"] == "telegram:dm:8078895371"

    @pytest.mark.asyncio
    async def test_send_non_telegram_still_rejected(self):
        adapter = _make_adapter()
        result = await adapter.send("not-a-tg-target", "hi")
        assert result.success is False
        assert "HTTP request/response" in (result.error or "")
