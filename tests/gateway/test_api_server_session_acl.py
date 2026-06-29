"""API-server multi-user session and response ACL tests."""

import asyncio
import json
import logging
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.api_server_shared import ResponseStore
from gateway.api_server_audit import REQUEST_ID_HEADER, request_audit_middleware
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware
from hermes_state import SessionDB


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def scoped_adapter(session_db):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    adapter._session_db = session_db
    adapter._response_store = ResponseStore(max_size=20, db_path=":memory:")
    return adapter


def _app(adapter: APIServerAdapter, *, audit: bool = False) -> web.Application:
    app = web.Application(middlewares=[request_audit_middleware] if audit else [])
    app["api_server_adapter"] = adapter
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_patch("/api/sessions/{session_id}", adapter._handle_patch_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/api/sessions/{session_id}/chat/stop", adapter._handle_session_chat_stop)
    app.router.add_post("/api/sessions/{session_id}/chat/approval", adapter._handle_session_chat_approval)
    app.router.add_post("/api/sessions/{session_id}/chat/prompt", adapter._handle_session_chat_prompt)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    app.router.add_delete("/v1/responses/{response_id}", adapter._handle_delete_response)
    return app


def _audit_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "gateway.api_server.audit"
    ]


def _cors_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return app


def _principal(user_id: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer sk-test",
        "X-Hermes-Tenant-Id": "tenant-1",
        "X-Hermes-Workspace-Id": "workspace-1",
        "X-Hermes-Project-Id": "project-1",
        "X-Hermes-User-Id": user_id,
    }


@pytest.mark.asyncio
async def test_api_server_audit_logs_invalid_api_key(scoped_adapter, caplog):
    app = _app(scoped_adapter, audit=True)
    caplog.set_level(logging.INFO, logger="gateway.api_server.audit")
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get(
            "/api/sessions",
            headers={
                "Authorization": "Bearer wrong",
                "X-Hermes-Request-Id": "req-auth-test",
            },
        )
        payload = await resp.json()

    assert resp.status == 401
    assert resp.headers[REQUEST_ID_HEADER] == "req-auth-test"
    assert payload["error"]["code"] == "invalid_api_key"
    joined = "\n".join(_audit_messages(caplog))
    assert "req-auth-test" in joined
    assert "auth.check" in joined
    assert "invalid_api_key" in joined
    assert "api.request" in joined
    assert "'status': 401" in joined


@pytest.mark.asyncio
async def test_api_server_audit_logs_cross_user_session_denial(scoped_adapter, caplog):
    app = _app(scoped_adapter, audit=True)
    caplog.set_level(logging.INFO, logger="gateway.api_server.audit")
    async with TestClient(TestServer(app)) as cli:
        create = await cli.post(
            "/api/sessions",
            headers=_principal("user-a"),
            json={"id": "audit-owned-by-a"},
        )
        assert create.status == 201
        caplog.clear()

        denied = await cli.get(
            "/api/sessions/audit-owned-by-a/messages",
            headers={**_principal("user-b"), "X-Hermes-Request-Id": "req-denied-test"},
        )
        payload = await denied.json()

    assert denied.status == 404
    assert denied.headers[REQUEST_ID_HEADER] == "req-denied-test"
    assert payload["error"]["code"] == "session_not_found"
    joined = "\n".join(_audit_messages(caplog))
    assert "req-denied-test" in joined
    assert "session.access" in joined
    assert "not_found_or_scope_denied" in joined
    assert "audit-owned-by-a" in joined
    assert "user-b" in joined


@pytest.mark.asyncio
async def test_session_resources_are_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)
    async with TestClient(TestServer(app)) as cli:
        create_a = await cli.post(
            "/api/sessions",
            headers=_principal("user-a"),
            json={"id": "session-a", "title": "A"},
        )
        create_b = await cli.post(
            "/api/sessions",
            headers=_principal("user-b"),
            json={"id": "session-b", "title": "B"},
        )
        assert create_a.status == 201
        assert create_b.status == 201

        list_a = await cli.get("/api/sessions", headers=_principal("user-a"))
        payload_a = await list_a.json()
        assert list_a.status == 200
        assert [item["id"] for item in payload_a["data"]] == ["session-a"]

        get_denied = await cli.get("/api/sessions/session-a", headers=_principal("user-b"))
        messages_denied = await cli.get("/api/sessions/session-a/messages", headers=_principal("user-b"))
        patch_denied = await cli.patch(
            "/api/sessions/session-a",
            headers=_principal("user-b"),
            json={"title": "stolen"},
        )
        fork_denied = await cli.post(
            "/api/sessions/session-a/fork",
            headers=_principal("user-b"),
            json={"id": "fork-b"},
        )
        delete_denied = await cli.delete("/api/sessions/session-a", headers=_principal("user-b"))

        assert get_denied.status == 404
        assert messages_denied.status == 404
        assert patch_denied.status == 404
        assert fork_denied.status == 404
        assert delete_denied.status == 404

        get_a = await cli.get("/api/sessions/session-a", headers=_principal("user-a"))
        assert get_a.status == 200


@pytest.mark.asyncio
async def test_session_chat_refuses_other_users_history(scoped_adapter, session_db):
    app = _app(scoped_adapter)
    async with TestClient(TestServer(app)) as cli:
        bind = await cli.post(
            "/api/sessions",
            headers=_principal("user-a"),
            json={"id": "owned-by-a"},
        )
        assert bind.status == 201
        session_db.append_message("owned-by-a", "user", "private prompt")
        session_db.append_message("owned-by-a", "assistant", "private answer")

        with patch.object(scoped_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = (
                {"final_response": "fresh", "session_id": "owned-by-a"},
                {"total_tokens": 1},
            )
            denied = await cli.post(
                "/api/sessions/owned-by-a/chat",
                headers=_principal("user-b"),
                json={"message": "continue"},
            )
            allowed = await cli.post(
                "/api/sessions/owned-by-a/chat",
                headers=_principal("user-a"),
                json={"message": "continue"},
            )

    assert denied.status == 404
    assert allowed.status == 200
    mock_run.assert_awaited_once()
    assert mock_run.await_args.kwargs["conversation_history"] == [
        {"role": "user", "content": "private prompt"},
        {"role": "assistant", "content": "private answer"},
    ]


@pytest.mark.asyncio
async def test_image_prompt_stream_routes_through_agent_inside_principal_scope(scoped_adapter, caplog):
    app = _app(scoped_adapter, audit=True)

    async def fake_run_agent(**kwargs):
        assert kwargs["user_message"] == "帮我生成一个猫的图片"
        kwargs["tool_start_callback"](
            "call-image-owned-by-a",
            "image_generate",
            {"prompt": "cat"},
        )
        kwargs["tool_progress_callback"](
            "tool.completed",
            tool_name="image_generate",
            preview="agent completed image tool",
            duration=0.2,
            result='{"success": true, "image": "https://static.atlascloud.ai/images/cat.png"}',
        )
        kwargs["tool_complete_callback"](
            "call-image-owned-by-a",
            "image_generate",
            {"prompt": "cat"},
            '{"success": true, "image": "https://static.atlascloud.ai/images/cat.png"}',
        )
        db = scoped_adapter._ensure_session_db()
        db.append_message("image-owned-by-a", "user", kwargs["user_message"])
        db.append_message("image-owned-by-a", "assistant", "agent routed image response")
        return (
            {
                "final_response": "agent routed image response",
                "session_id": "image-owned-by-a",
                "messages": [
                    {"role": "user", "content": kwargs["user_message"]},
                    {"role": "assistant", "content": "agent routed image response"},
                ],
            },
            {"total_tokens": 3},
        )

    with patch.object(scoped_adapter, "_run_agent", side_effect=fake_run_agent) as mock_run:
        caplog.set_level(logging.INFO, logger="gateway.api_server.audit")
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "image-owned-by-a", "title": "Image"},
            )
            assert create.status == 201

            stream = await cli.post(
                "/api/sessions/image-owned-by-a/chat/stream",
                headers={**_principal("user-a"), "X-Hermes-Request-Id": "req-stream-test"},
                json={"message": "帮我生成一个猫的图片"},
            )
            assert stream.status == 200, await stream.text()
            assert stream.headers[REQUEST_ID_HEADER] == "req-stream-test"
            body = await stream.text()

            messages_a = await cli.get(
                "/api/sessions/image-owned-by-a/messages",
                headers=_principal("user-a"),
            )
            messages_b = await cli.get(
                "/api/sessions/image-owned-by-a/messages",
                headers=_principal("user-b"),
            )
            assert messages_a.status == 200
            payload_a = await messages_a.json()
            assert messages_b.status == 404

    mock_run.assert_awaited_once()
    assert "event: tool.started" in body
    assert "event: assistant.completed" in body
    assert "agent routed image response" in body
    assert [item["role"] for item in payload_a["data"]] == ["user", "assistant"]
    assert payload_a["data"][1]["content"] == "agent routed image response"
    joined = "\n".join(_audit_messages(caplog))
    assert "req-stream-test" in joined
    assert "session.chat_stream" in joined
    assert "direct_" + "image" not in joined


@pytest.mark.asyncio
async def test_session_chat_stream_emits_tool_lifecycle_events(scoped_adapter):
    app = _app(scoped_adapter)

    async def fake_run_agent(**kwargs):
        kwargs["stream_delta_callback"]("hello ")
        kwargs["tool_start_callback"](
            "call-stream-image",
            "image_generate",
            {"prompt": "cat"},
        )
        kwargs["tool_progress_callback"](
            "reasoning.available",
            tool_name="_thinking",
            preview="Planning",
        )
        kwargs["tool_progress_callback"](
            "tool.completed",
            tool_name="image_generate",
            preview="Created image",
            duration=1.25,
            result='{"success": true, "image": "https://static.atlascloud.ai/images/cat.png"}',
        )
        kwargs["tool_complete_callback"](
            "call-stream-image",
            "image_generate",
            {"prompt": "cat"},
            '{"success": true, "image": "https://static.atlascloud.ai/images/cat.png"}',
        )
        kwargs["prompt_notify_callback"]({
            "kind": "clarify",
            "request_id": "clarify-stream-tools",
            "question": "Pick one",
            "choices": ["A", "B"],
        })
        return (
            {"final_response": "hello done", "session_id": "stream-tools"},
            {"total_tokens": 2},
        )

    with patch.object(scoped_adapter, "_run_agent", side_effect=fake_run_agent):
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "stream-tools", "title": "Tools"},
            )
            assert create.status == 201

            stream = await cli.post(
                "/api/sessions/stream-tools/chat/stream",
                headers=_principal("user-a"),
                json={"message": "run a tool"},
            )
            body = await stream.text()

    assert stream.status == 200
    assert "event: assistant.delta" in body
    assert "event: tool.started" in body
    assert "event: tool.progress" in body
    assert "event: tool.completed" in body
    assert '"tool_call_id": "call-stream-image"' in body
    assert '"duration_ms": 1250' in body
    assert "https://static.atlascloud.ai/images/cat.png" in body
    assert "event: clarify.request" in body
    assert "hello done" in body


@pytest.mark.asyncio
async def test_session_chat_stream_emits_failed_tool_when_result_is_error(scoped_adapter):
    app = _app(scoped_adapter)

    async def fake_run_agent(**kwargs):
        kwargs["tool_start_callback"](
            "call-failed-image",
            "image_generate",
            {"prompt": "cat"},
        )
        kwargs["tool_progress_callback"](
            "tool.completed",
            tool_name="image_generate",
            result=json.dumps({
                "success": False,
                "error": "Tool blocked by policy",
                "reason": "insufficient_role",
            }),
            is_error=True,
            duration=0.5,
        )
        kwargs["tool_complete_callback"](
            "call-failed-image",
            "image_generate",
            {"prompt": "cat"},
            json.dumps({
                "success": False,
                "error": "Tool blocked by policy",
                "reason": "insufficient_role",
            }),
        )
        return (
            {"final_response": "could not generate", "session_id": "failed-tool"},
            {"total_tokens": 2},
        )

    with patch.object(scoped_adapter, "_run_agent", side_effect=fake_run_agent):
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "failed-tool", "title": "Failed tool"},
            )
            assert create.status == 201

            stream = await cli.post(
                "/api/sessions/failed-tool/chat/stream",
                headers=_principal("user-a"),
                json={"message": "帮我生成一个猫的图片"},
            )
            body = await stream.text()

    assert stream.status == 200
    assert "event: tool.started" in body
    assert "event: tool.failed" in body
    assert '"tool_call_id": "call-failed-image"' in body
    assert "Tool blocked by policy" in body
    assert "insufficient_role" in body


@pytest.mark.asyncio
async def test_session_chat_stop_is_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)

    class FakeAgent:
        interrupted = ""

        def interrupt(self, message):
            self.interrupted = message

    async def sleeper():
        await asyncio.sleep(30)

    fake_agent = FakeAgent()
    task = asyncio.create_task(sleeper())
    try:
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "stop-owned", "title": "Stop"},
            )
            assert create.status == 201
            scoped_adapter._active_session_streams["stop-owned"] = {
                "task": task,
                "agent_ref": [fake_agent],
                "run_id": "run-stop-owned",
                "principal_scope": {},
            }

            denied = await cli.post(
                "/api/sessions/stop-owned/chat/stop",
                headers=_principal("user-b"),
            )
            allowed = await cli.post(
                "/api/sessions/stop-owned/chat/stop",
                headers=_principal("user-a"),
            )
            payload = await allowed.json()
    finally:
        task.cancel()

    assert denied.status == 404
    assert allowed.status == 200
    assert payload["status"] == "stopping"
    assert payload["run_id"] == "run-stop-owned"
    assert fake_agent.interrupted == "Stop requested via API"


@pytest.mark.asyncio
async def test_session_chat_approval_is_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)

    with patch("tools.approval.resolve_gateway_approval", return_value=1) as mock_resolve:
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "approval-owned", "title": "Approval"},
            )
            assert create.status == 201
            scoped_adapter._active_session_streams["approval-owned"] = {
                "approval_session_key": "approval-key",
                "run_id": "run-approval-owned",
            }

            denied = await cli.post(
                "/api/sessions/approval-owned/chat/approval",
                headers=_principal("user-b"),
                json={"choice": "once"},
            )
            allowed = await cli.post(
                "/api/sessions/approval-owned/chat/approval",
                headers=_principal("user-a"),
                json={"choice": "approve"},
            )
            payload = await allowed.json()

    assert denied.status == 404
    assert allowed.status == 200
    assert payload["choice"] == "once"
    assert payload["resolved"] == 1
    mock_resolve.assert_called_once_with("approval-key", "once", resolve_all=False)


@pytest.mark.asyncio
async def test_session_chat_prompt_is_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)
    from tools.clarify_gateway import clear_session, register

    register("prompt-owned-1", "prompt-key", "Need input?", None)
    try:
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "prompt-owned", "title": "Prompt"},
            )
            assert create.status == 201
            scoped_adapter._active_session_streams["prompt-owned"] = {
                "prompt_session_key": "prompt-key",
                "prompt_request_ids": {"prompt-owned-1"},
                "run_id": "run-prompt-owned",
            }

            denied = await cli.post(
                "/api/sessions/prompt-owned/chat/prompt",
                headers=_principal("user-b"),
                json={"request_id": "prompt-owned-1", "answer": "nope"},
            )
            allowed = await cli.post(
                "/api/sessions/prompt-owned/chat/prompt",
                headers=_principal("user-a"),
                json={"request_id": "prompt-owned-1", "answer": "yes"},
            )
            payload = await allowed.json()
    finally:
        clear_session("prompt-key")

    assert denied.status == 404
    assert allowed.status == 200
    assert payload["resolved"] is True
    assert payload["request_id"] == "prompt-owned-1"


@pytest.mark.asyncio
async def test_default_chat_session_id_includes_principal_fingerprint(scoped_adapter):
    app = _app(scoped_adapter)
    with patch.object(scoped_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "ok", "messages": []},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            body = {
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "same prompt"}],
            }
            first_a = await cli.post("/v1/chat/completions", headers=_principal("user-a"), json=body)
            second_a = await cli.post("/v1/chat/completions", headers=_principal("user-a"), json=body)
            first_b = await cli.post("/v1/chat/completions", headers=_principal("user-b"), json=body)

    assert first_a.status == 200
    assert second_a.status == 200
    assert first_b.status == 200
    assert first_a.headers["X-Hermes-Session-Id"] == second_a.headers["X-Hermes-Session-Id"]
    assert first_a.headers["X-Hermes-Session-Id"] != first_b.headers["X-Hermes-Session-Id"]


@pytest.mark.asyncio
async def test_responses_state_is_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)
    with patch.object(scoped_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "answer", "messages": []},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/v1/responses",
                headers=_principal("user-a"),
                json={"input": "secret"},
            )
            assert create.status == 200
            response_id = (await create.json())["id"]

            get_denied = await cli.get(f"/v1/responses/{response_id}", headers=_principal("user-b"))
            chain_denied = await cli.post(
                "/v1/responses",
                headers=_principal("user-b"),
                json={"input": "continue", "previous_response_id": response_id},
            )
            delete_denied = await cli.delete(f"/v1/responses/{response_id}", headers=_principal("user-b"))
            get_allowed = await cli.get(f"/v1/responses/{response_id}", headers=_principal("user-a"))
            delete_allowed = await cli.delete(f"/v1/responses/{response_id}", headers=_principal("user-a"))

    assert get_denied.status == 404
    assert chain_denied.status == 404
    assert delete_denied.status == 404
    assert get_allowed.status == 200
    assert delete_allowed.status == 200
    assert mock_run.await_count == 1


@pytest.mark.asyncio
async def test_scoped_conversation_names_do_not_cross_users(scoped_adapter):
    app = _app(scoped_adapter)
    with patch.object(scoped_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "answer", "messages": []},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            first = await cli.post(
                "/v1/responses",
                headers=_principal("user-a"),
                json={"input": "first", "conversation": "shared-name"},
            )
            second = await cli.post(
                "/v1/responses",
                headers=_principal("user-b"),
                json={"input": "second", "conversation": "shared-name"},
            )

    assert first.status == 200
    assert second.status == 200
    assert mock_run.await_count == 2
    assert mock_run.await_args_list[0].kwargs["conversation_history"] == []
    assert mock_run.await_args_list[1].kwargs["conversation_history"] == []


@pytest.mark.asyncio
async def test_idempotency_cache_is_scoped_to_principal(scoped_adapter):
    app = _app(scoped_adapter)
    with patch.object(scoped_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (
            {"final_response": "answer", "messages": []},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
        async with TestClient(TestServer(app)) as cli:
            key = f"idem-{uuid.uuid4().hex}"
            body = {
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "same request"}],
            }
            first = await cli.post(
                "/v1/chat/completions",
                headers={**_principal("user-a"), "Idempotency-Key": key},
                json=body,
            )
            second = await cli.post(
                "/v1/chat/completions",
                headers={**_principal("user-b"), "Idempotency-Key": key},
                json=body,
            )

    assert first.status == 200
    assert second.status == 200
    assert mock_run.await_count == 2


@pytest.mark.asyncio
async def test_cors_allows_principal_scope_headers(session_db):
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={"key": "sk-test", "cors_origins": ["http://127.0.0.1:9132"]},
        )
    )
    adapter._session_db = session_db
    app = _cors_app(adapter)

    async with TestClient(TestServer(app)) as cli:
        resp = await cli.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://127.0.0.1:9132",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": (
                    "authorization,content-type,x-hermes-tenant-id,"
                    "x-hermes-workspace-id,x-hermes-project-id,x-hermes-user-id"
                ),
            },
        )

    assert resp.status == 200
    allow = resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "x-hermes-tenant-id" in allow
    assert "x-hermes-workspace-id" in allow
    assert "x-hermes-project-id" in allow
    assert "x-hermes-user-id" in allow
