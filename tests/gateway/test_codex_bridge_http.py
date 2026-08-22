from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from gateway.codex_bridge import (
    BridgeExecutionResult,
    BridgeStore,
    CodexBridgeService,
    CodexBridgeSettings,
    CodexUserQuestion,
    GatewayCodexBridgeMixin,
)
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


API_KEY = "phase1-http-key-that-is-long-enough"
AUTH = {"Authorization": f"Bearer {API_KEY}", "X-Hermes-Session-Key": "client-a"}


class QuestionExecutor:
    def __init__(self):
        self.calls = []

    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "thread-http-phase1")
        raise CodexUserQuestion(
            "Environment: Choose the deployment environment.\n"
            "Lựa chọn: Staging (Safe); Production (User-impacting)"
        )


class ArtifactExecutor:
    def __init__(self, artifact: str):
        self.artifact = artifact
        self.calls = []

    def execute(self, request, *, codex_thread_id, on_thread, on_progress):
        self.calls.append(codex_thread_id)
        on_thread(codex_thread_id or "unexpected-new-thread")
        return BridgeExecutionResult(
            "Staging selected; artifact is ready.", (self.artifact,)
        )


class HttpBridgeRunner(GatewayCodexBridgeMixin):
    def __init__(self, settings, service, adapter):
        self.settings = settings
        self._codex_bridge_service = service
        self.adapter = adapter

    def _codex_bridge_settings(self):
        return self.settings

    def _adapter_for_source(self, _source):
        return self.adapter


def _settings(workspace) -> CodexBridgeSettings:
    return CodexBridgeSettings(
        enabled=True,
        allowed_origins=("api_server",),
        workspace_allowlist=(str(workspace),),
        sandbox="read-only",
        stale_recovery_seconds=1,
    )


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/codex/tasks", adapter._handle_codex_task_start)
    app.router.add_get(
        "/v1/codex/tasks/{task_id}", adapter._handle_codex_task_get
    )
    app.router.add_post(
        "/v1/codex/tasks/{task_id}/reply", adapter._handle_codex_task_reply
    )
    return app


async def _wait_for_phase(client, task_id, phase, *, headers=AUTH):
    for _ in range(200):
        response = await client.get(f"/v1/codex/tasks/{task_id}", headers=headers)
        payload = await response.json()
        if payload.get("phase") == phase:
            return response, payload
        await asyncio.sleep(0.01)
    pytest.fail(f"task {task_id} did not reach {phase}")


@pytest.mark.asyncio
async def test_authenticated_http_question_restart_reply_and_artifact(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "phase1-result.txt"
    artifact.write_text("verified artifact", encoding="utf-8")
    store_path = tmp_path / "bridge.db"
    settings = _settings(workspace)
    question_executor = QuestionExecutor()
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": API_KEY})
    )
    first_service = CodexBridgeService(
        settings,
        store=BridgeStore(store_path),
        executor=question_executor,
        instance_id="http-before-restart",
    )
    first_runner = HttpBridgeRunner(settings, first_service, adapter)
    adapter.gateway_runner = first_runner
    client = TestClient(TestServer(_app(adapter)))
    await client.start_server()
    try:
        unauthorized = await client.post(
            "/v1/codex/tasks",
            json={"input": "deploy", "workspace": str(workspace)},
            headers={"Idempotency-Key": "http-initial"},
        )
        assert unauthorized.status == 401

        initial_headers = {**AUTH, "Idempotency-Key": "http-initial"}
        initial = await client.post(
            "/v1/codex/tasks",
            json={"input": "deploy", "workspace": str(workspace)},
            headers=initial_headers,
        )
        initial_payload = await initial.json()
        assert initial.status == 202
        assert initial_payload["phase"] in {"captured", "working"}
        task_id = initial_payload["task_id"]
        _question_response, question_payload = await _wait_for_phase(
            client, task_id, "needs_user"
        )
        duplicate = await client.post(
            "/v1/codex/tasks",
            json={"input": "deploy", "workspace": str(workspace)},
            headers=initial_headers,
        )
        duplicate_payload = await duplicate.json()

        assert duplicate.status == 202
        assert question_payload["prompt_id"].startswith("prompt_")
        assert question_payload["question"] == duplicate_payload["question"]
        assert question_executor.calls == [None]
        prompt_id = question_payload["prompt_id"]

        changed_duplicate = await client.post(
            "/v1/codex/tasks",
            json={"input": "deploy production", "workspace": str(workspace)},
            headers=initial_headers,
        )
        assert changed_duplicate.status == 409
        assert (await changed_duplicate.json())["error"]["code"] == "idempotency_conflict"
        assert question_executor.calls == [None]

        cross_origin_get = await client.get(
            f"/v1/codex/tasks/{task_id}",
            headers={**AUTH, "X-Hermes-Session-Key": "client-b"},
        )
        assert cross_origin_get.status == 404
        cross_origin_duplicate = await client.post(
            "/v1/codex/tasks",
            json={"input": "deploy", "workspace": str(workspace)},
            headers={
                **initial_headers,
                "X-Hermes-Session-Key": "client-b",
            },
        )
        cross_origin_payload = await cross_origin_duplicate.json()
        assert cross_origin_duplicate.status == 409
        assert "question" not in cross_origin_payload

        resumed_executor = ArtifactExecutor(str(artifact.resolve()))
        restarted_service = CodexBridgeService(
            settings,
            store=BridgeStore(store_path),
            executor=resumed_executor,
            instance_id="http-after-restart",
        )
        restarted_runner = HttpBridgeRunner(
            settings, restarted_service, adapter
        )
        adapter.gateway_runner = restarted_runner

        status_response = await client.get(
            f"/v1/codex/tasks/{task_id}", headers=AUTH
        )
        assert status_response.status == 200
        assert (await status_response.json())["prompt_id"] == prompt_id

        wrong_origin = await client.post(
            f"/v1/codex/tasks/{task_id}/reply",
            json={"prompt_id": prompt_id, "answer": "Staging"},
            headers={
                **AUTH,
                "X-Hermes-Session-Key": "client-b",
                "Idempotency-Key": "http-wrong-origin",
            },
        )
        assert wrong_origin.status == 409
        assert "question" not in await wrong_origin.json()
        assert resumed_executor.calls == []

        reply_headers = {**AUTH, "Idempotency-Key": "http-reply"}
        reply = await client.post(
            f"/v1/codex/tasks/{task_id}/reply",
            json={"prompt_id": prompt_id, "answer": "Staging"},
            headers=reply_headers,
        )
        reply_payload = await reply.json()
        duplicate_reply = await client.post(
            f"/v1/codex/tasks/{task_id}/reply",
            json={"prompt_id": prompt_id, "answer": "Staging"},
            headers=reply_headers,
        )
        duplicate_reply_payload = await duplicate_reply.json()

        assert reply.status == duplicate_reply.status == 200
        assert reply_payload["phase"] == "done"
        assert reply_payload["result"] == duplicate_reply_payload["result"]
        assert reply_payload["artifacts"] == [str(artifact.resolve())]
        assert resumed_executor.calls == ["thread-http-phase1"]
        assert restarted_service.store is not first_service.store
        assert [event["phase"] for event in reply_payload["events"]] == [
            "captured",
            "working",
            "needs_user",
            "working",
            "output_ready",
            "done",
        ]
    finally:
        await client.close()
        await adapter.disconnect()


def test_api_server_route_table_advertises_codex_bridge_surface():
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": API_KEY})
    )
    routes = {(method, path) for method, path, _handler in adapter._http_route_table()}
    assert ("POST", "/v1/codex/tasks") in routes
    assert ("GET", "/v1/codex/tasks/{task_id}") in routes
    assert ("POST", "/v1/codex/tasks/{task_id}/reply") in routes
    adapter._response_store.close()
