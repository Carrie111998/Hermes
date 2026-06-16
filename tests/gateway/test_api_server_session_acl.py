"""API-server multi-user session and response ACL tests."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.api_server_shared import ResponseStore
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


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/api/sessions", adapter._handle_list_sessions)
    app.router.add_post("/api/sessions", adapter._handle_create_session)
    app.router.add_get("/api/sessions/{session_id}", adapter._handle_get_session)
    app.router.add_patch("/api/sessions/{session_id}", adapter._handle_patch_session)
    app.router.add_delete("/api/sessions/{session_id}", adapter._handle_delete_session)
    app.router.add_get("/api/sessions/{session_id}/messages", adapter._handle_session_messages)
    app.router.add_post("/api/sessions/{session_id}/fork", adapter._handle_fork_session)
    app.router.add_post("/api/sessions/{session_id}/chat", adapter._handle_session_chat)
    app.router.add_post("/api/sessions/{session_id}/chat/stream", adapter._handle_session_chat_stream)
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/responses/{response_id}", adapter._handle_get_response)
    app.router.add_delete("/v1/responses/{response_id}", adapter._handle_delete_response)
    return app


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
async def test_direct_atlas_image_stream_persists_inside_principal_scope(scoped_adapter):
    app = _app(scoped_adapter)
    fake_result = {
        "success": True,
        "image": "https://atlas-media.example/images/cat.png",
        "model": "google/nano-banana-2/text-to-image",
    }

    with patch("gateway.api_server_sessions.generate_atlas_image", return_value=fake_result) as mock_generate:
        async with TestClient(TestServer(app)) as cli:
            create = await cli.post(
                "/api/sessions",
                headers=_principal("user-a"),
                json={"id": "image-owned-by-a", "title": "Image"},
            )
            assert create.status == 201

            stream = await cli.post(
                "/api/sessions/image-owned-by-a/chat/stream",
                headers=_principal("user-a"),
                json={"message": "帮我生成一个猫的图片"},
            )
            assert stream.status == 200, await stream.text()
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

    mock_generate.assert_called_once_with("帮我生成一个猫的图片")
    assert "event: tool.started" in body
    assert "event: assistant.completed" in body
    assert "https://atlas-media.example/images/cat.png" in body
    assert [item["role"] for item in payload_a["data"]] == ["user", "assistant"]
    assert "https://atlas-media.example/images/cat.png" in payload_a["data"][1]["content"]


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
