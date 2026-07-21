from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from gateway.api_server_runtime import APIServerRuntimeMixin, _runtime_tool_middleware

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class _RuntimeAdapter(APIServerRuntimeMixin):
    def _check_auth(self, _request):
        return None

    async def _run_agent(self, **kwargs):
        agent = SimpleNamespace(
            tools=[{
                "type": "function",
                "function": {"name": "skill_view", "description": "", "parameters": {"type": "object"}},
            }],
            valid_tool_names={"skill_view"},
            model="configured-model",
        )
        kwargs["agent_configurator"](agent)
        assert kwargs["ephemeral_system_prompt"] is None
        assert agent.model == "chat-test"
        assert agent.valid_tool_names == {"skill_view", "ultra_media_job_create"}
        assert agent.ephemeral_system_prompt is None
        assert agent._cached_system_prompt == "platform rules\n\ntrusted turn context"
        assert agent._build_system_prompt() == agent._cached_system_prompt

        skill_result = _runtime_tool_middleware(
            tool_name="skill_view",
            args={"name": "workflow-router"},
            session_id=kwargs["session_id"],
            tool_call_id="skill_call",
            next_call=lambda _args: "workflow instructions",
        )
        assert skill_result == "workflow instructions"

        tool_result = await asyncio.to_thread(
            _runtime_tool_middleware,
            tool_name="ultra_media_job_create",
            args={"operation": "image.generate", "prompt": "test"},
            session_id=kwargs["session_id"],
            tool_call_id="call_01",
            next_call=lambda _args: pytest.fail("platform tool executed inside Hermes"),
        )
        assert json.loads(tool_result) == {"job_id": "job_01"}
        return {"final_response": "asset://image/01"}, {"total_tokens": 3}


@pytest.mark.asyncio
async def test_runtime_driver_streams_tool_request_and_waits_for_result():
    adapter = _RuntimeAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    app.router.add_post("/v1/runtime/runs/{run_id}/tool-results", adapter._handle_runtime_tool_result)
    app.router.add_post("/v1/runtime/runs/{run_id}/interrupt", adapter._handle_runtime_interrupt)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        version = "ultrastudio-supercomputer/v1"
        mode = "replace"
        stable = "platform rules"
        turn = "trusted turn context"
        digest = "sha256:" + hashlib.sha256(
            f"{version}\n{mode}\n{stable}\n{turn}".encode("utf-8"),
        ).hexdigest()
        response = await client.post("/v1/runtime/runs", json={
            "run_id": "run_test",
            "runtime": "hermes",
            "model": "chat-test",
            "context": {"session_id": "panel_session_test"},
            "messages": [{"role": "user", "content": "make an image"}],
            "system_context": {
                "version": version,
                "mode": mode,
                "digest": digest,
                "stable": stable,
                "turn": turn,
            },
            "tools": [{
                "name": "ultra_media_job_create",
                "description": "create media",
                "input_schema": {"type": "object", "properties": {}},
                "route": "tokenrouter",
                "required_skill": "workflow-router",
            }],
        })
        assert response.status == 200
        started = json.loads(await response.content.readline())
        assert started["type"] == "run_started"
        assert started["payload"]["system_context_version"] == version
        assert started["payload"]["system_context_mode"] == "replace"
        assert started["payload"]["system_context_digest"] == digest
        tool_request = json.loads(await response.content.readline())
        assert tool_request["type"] == "tool_request"
        assert tool_request["payload"]["skill"]["name"] == "workflow-router"
        assert tool_request["payload"]["skill"]["digest"].startswith("sha256:")

        delivered = await client.post("/v1/runtime/runs/run_test/tool-results", json={
            "call_id": "call_01",
            "ok": True,
            "result": {"job_id": "job_01"},
        })
        assert delivered.status == 204
        usage = json.loads(await response.content.readline())
        completed = json.loads(await response.content.readline())
        assert usage["type"] == "usage"
        assert completed["type"] == "completed"
        assert completed["payload"]["text"] == "asset://image/01"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_driver_rejects_non_replacement_or_tampered_prompt():
    adapter = _RuntimeAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = {
            "run_id": "run_bad_prompt",
            "runtime": "hermes",
            "model": "chat-test",
            "context": {"session_id": "panel_session_bad"},
            "messages": [{"role": "user", "content": "hello"}],
            "system_context": {
                "version": "ultrastudio-supercomputer/v1",
                "mode": "append",
                "digest": "sha256:bad",
                "stable": "platform rules",
            },
        }
        response = await client.post("/v1/runtime/runs", json=body)
        assert response.status == 422
        payload = await response.json()
        assert "replacement" in payload["error"]["message"]

        body["system_context"]["mode"] = "replace"
        response = await client.post("/v1/runtime/runs", json=body)
        assert response.status == 422
        payload = await response.json()
        assert "digest mismatch" in payload["error"]["message"]
    finally:
        await client.close()
