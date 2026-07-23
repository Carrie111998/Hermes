from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
import gateway.api_server_runtime as runtime_module

from gateway.api_server_runtime import (
    APIServerRuntimeMixin,
    RuntimeBridgeSession,
    _pin_run_model,
    _resume_runtime_history,
    _runtime_tool_middleware,
)

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class _RuntimeAdapter(APIServerRuntimeMixin):
    def _check_auth(self, _request):
        return None

    async def _run_agent(self, **kwargs):
        agent = SimpleNamespace(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "skill_view", "description": "", "parameters": {"type": "object"}},
                },
                {
                    "type": "function",
                    "function": {"name": "skills_list", "description": "", "parameters": {"type": "object"}},
                },
            ],
            valid_tool_names={"skill_view", "skills_list"},
            model="configured-model",
            _primary_runtime={
                "model": "configured-model",
                "compressor_model": "configured-model",
            },
            _fallback_chain=[{"provider": "other", "model": "fallback-model"}],
            _fallback_model={"provider": "other", "model": "fallback-model"},
            _fallback_index=0,
            _fallback_activated=False,
        )
        kwargs["agent_configurator"](agent)
        assert kwargs["ephemeral_system_prompt"] is None
        assert agent.model == "chat-test"
        assert agent._run_model_pin == "chat-test"
        assert agent._primary_runtime["model"] == "chat-test"
        assert agent._primary_runtime["compressor_model"] == "chat-test"
        assert agent._fallback_chain == []
        assert agent._fallback_model is None
        assert agent.valid_tool_names == {"skill_view", "ultra_media_job_create"}
        assert agent.ephemeral_system_prompt is None
        assert agent._cached_system_prompt == (
            "platform rules\n\ntrusted turn context\n\n"
            "<available_skills>\n"
            "- media-qa: Inspect generated media.\n"
            "</available_skills>"
        )
        assert agent._build_system_prompt() == agent._cached_system_prompt

        kwargs["tool_start_callback"]("skill_call", "skill_view", {
            "name": "media-qa",
            "task_id": "must-not-cross-runtime-boundary",
        })
        skill_result = _runtime_tool_middleware(
            tool_name="skill_view",
            args={"name": "media-qa"},
            session_id=kwargs["session_id"],
            tool_call_id="skill_call",
            next_call=lambda _args: "workflow instructions",
        )
        assert skill_result == "workflow instructions"
        kwargs["tool_complete_callback"](
            "skill_call",
            "skill_view",
            {"name": "media-qa"},
            json.dumps({"success": True, "content": "workflow instructions"}),
        )

        denied = _runtime_tool_middleware(
            tool_name="skill_view",
            args={"name": "tv-ad"},
            session_id=kwargs["session_id"],
            tool_call_id="draft_skill_call",
            next_call=lambda _args: pytest.fail("draft skill reached native skill_view"),
        )
        assert json.loads(denied) == {
            "success": False,
            "error": "Skill 'tv-ad' is not available for this run.",
        }

        agent._runtime_checkpoint_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_01",
                "type": "function",
                "function": {
                    "name": "ultra_media_job_create",
                    "arguments": '{"operation":"image.generate","prompt":"test"}',
                },
            }],
        }
        kwargs["tool_start_callback"]("call_01", "ultra_media_job_create", {"prompt": "test"})
        tool_result = await asyncio.to_thread(
            _runtime_tool_middleware,
            tool_name="ultra_media_job_create",
            args={"operation": "image.generate", "prompt": "test"},
            session_id=kwargs["session_id"],
            tool_call_id="call_01",
            next_call=lambda _args: pytest.fail("platform tool executed inside Hermes"),
        )
        assert json.loads(tool_result) == {"job_id": "job_01"}
        kwargs["tool_complete_callback"]("call_01", "ultra_media_job_create", {}, tool_result)
        return {"final_response": "asset://image/01"}, {"total_tokens": 3}


def test_pin_run_model_uses_canonical_switch_and_disables_fallbacks():
    calls = []

    def switch_model(model, provider, api_key, base_url, api_mode):
        calls.append((model, provider, api_key, base_url, api_mode))
        agent.model = model

    agent = SimpleNamespace(
        model="anthropic/claude-opus-4.8",
        provider="custom",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        api_mode="chat_completions",
        switch_model=switch_model,
        _primary_runtime={
            "model": "anthropic/claude-opus-4.8",
            "compressor_model": "anthropic/claude-opus-4.8",
        },
        _fallback_chain=[{"provider": "custom", "model": "zai-org/glm-5.2"}],
        _fallback_model={"provider": "custom", "model": "zai-org/glm-5.2"},
        _fallback_index=1,
        _fallback_activated=True,
    )

    pinned = _pin_run_model(agent, "anthropic/claude-opus-4.6")

    assert pinned == "anthropic/claude-opus-4.6"
    assert calls == [(
        "anthropic/claude-opus-4.6",
        "custom",
        "test-key",
        "https://example.invalid/v1",
        "chat_completions",
    )]
    assert agent.model == pinned
    assert agent._run_model_pin == pinned
    assert agent._primary_runtime["model"] == pinned
    assert agent._primary_runtime["compressor_model"] == pinned
    assert agent._fallback_chain == []
    assert agent._fallback_model is None
    assert agent._fallback_index == 0
    assert agent._fallback_activated is False


@pytest.mark.asyncio
async def test_runtime_driver_streams_tool_request_and_waits_for_result(monkeypatch):
    monkeypatch.setattr(runtime_module, "_discover_skill_metadata", lambda: [
        {"name": "media-qa", "description": "Inspect generated media.", "category": "creative"},
    ])
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
                "allowed_skills": ["media-qa"],
            }],
        })
        assert response.status == 200
        started = json.loads(await response.content.readline())
        assert started["type"] == "run_started"
        assert started["payload"]["system_context_version"] == version
        assert started["payload"]["system_context_mode"] == "replace"
        assert started["payload"]["system_context_digest"] == digest
        activity_started = json.loads(await response.content.readline())
        assert activity_started == {
            "run_id": "run_test",
            "type": "activity_started",
            "payload": {
                "call_id": "skill_call",
                "name": "skill_view",
                "arguments": {"name": "media-qa"},
            },
        }
        activity_completed = json.loads(await response.content.readline())
        assert activity_completed == {
            "run_id": "run_test",
            "type": "activity_completed",
            "payload": {
                "call_id": "skill_call",
                "name": "skill_view",
                "status": "completed",
            },
        }
        checkpoint = json.loads(await response.content.readline())
        assert checkpoint["type"] == "checkpoint"
        assert checkpoint["payload"]["message"]["tool_calls"][0]["id"] == "call_01"
        tool_request = json.loads(await response.content.readline())
        assert tool_request["type"] == "tool_request"
        assert "skill" not in tool_request["payload"]
        assert "skills" not in tool_request["payload"]

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


def test_resume_history_continues_from_tool_result_without_synthetic_user():
    history = _resume_runtime_history(
        [{"role": "user", "content": "make an image"}],
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_media",
                    "type": "function",
                    "function": {
                        "name": "media.generate_image",
                        "arguments": '{"requests":[{"model":"image-model","prompt":"cat"}]}',
                    },
                }],
            },
        },
        [{
            "tool_call_id": "call_media",
            "status": "succeeded",
            "output": {"batch_status": "succeeded", "jobs": [{"job_id": "job_1"}]},
        }],
    )
    assert [message["role"] for message in history] == ["user", "assistant", "tool"]
    assert history[-1]["tool_call_id"] == "call_media"
    assert json.loads(history[-1]["content"])["batch_status"] == "succeeded"
    assert sum(message["role"] == "user" for message in history) == 1


def test_resume_history_rejects_checkpoint_with_multiple_platform_calls():
    checkpoint = {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "function": {"name": "one", "arguments": "{}"}},
                {"id": "call_2", "function": {"name": "two", "arguments": "{}"}},
            ],
        },
    }
    with pytest.raises(ValueError, match="exactly one platform tool call"):
        _resume_runtime_history(
            [{"role": "user", "content": "go"}],
            checkpoint,
            [{"tool_call_id": "call_1", "status": "succeeded", "output": {}}],
        )


@pytest.mark.asyncio
async def test_runtime_resume_wiring_reaches_agent_without_new_user_message():
    class ResumeAdapter(APIServerRuntimeMixin):
        _api_key = ""

        def _check_auth(self, _request):
            return None

        async def _run_agent(self, **kwargs):
            agent = SimpleNamespace(tools=[], valid_tool_names=set(), model="configured-model")
            kwargs["agent_configurator"](agent)
            assert agent._resume_from_tool_results is True
            assert kwargs["user_message"] == ""
            assert [message["role"] for message in kwargs["conversation_history"]] == [
                "user", "assistant", "tool",
            ]
            return {"final_response": "image complete"}, {"total_tokens": 2}

    adapter = ResumeAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        stable = "platform rules"
        version = "resume/v1"
        digest = "sha256:" + hashlib.sha256(
            f"{version}\nreplace\n{stable}\n".encode(),
        ).hexdigest()
        response = await client.post("/v1/runtime/runs", json={
            "run_id": "run_resume",
            "model": "chat-test",
            "messages": [{"role": "user", "content": "make an image"}],
            "system_context": {
                "version": version,
                "mode": "replace",
                "stable": stable,
                "turn": "",
                "digest": digest,
            },
            "runtime_checkpoint": {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_media",
                        "function": {"name": "media.generate_image", "arguments": "{}"},
                    }],
                },
            },
            "tool_results": [{
                "tool_call_id": "call_media",
                "status": "succeeded",
                "output": {"batch_status": "succeeded"},
            }],
        })
        assert response.status == 200
        events = [json.loads(line) async for line in response.content]
        assert events[-1]["type"] == "completed"
        assert events[-1]["payload"]["text"] == "image complete"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_driver_reports_skill_failure_without_result_content():
    class FailingSkillAdapter(_RuntimeAdapter):
        async def _run_agent(self, **kwargs):
            agent = SimpleNamespace(tools=[], valid_tool_names=set(), model="configured-model")
            kwargs["agent_configurator"](agent)
            kwargs["tool_start_callback"]("skill_failed", "skill_view", {
                "name": "missing-skill",
                "file_path": "SKILL.md",
            })
            kwargs["tool_complete_callback"](
                "skill_failed",
                "skill_view",
                {"name": "missing-skill"},
                json.dumps({
                    "success": False,
                    "error": "Skill 'missing-skill' not found.",
                    "available_skills": ["private-skill-name"],
                }),
            )
            return {"final_response": "could not load skill"}, {}

    adapter = FailingSkillAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        version = "ultrastudio-supercomputer/v1"
        mode = "replace"
        stable = "platform rules"
        turn = ""
        digest = "sha256:" + hashlib.sha256(
            f"{version}\n{mode}\n{stable}\n{turn}".encode("utf-8"),
        ).hexdigest()
        response = await client.post("/v1/runtime/runs", json={
            "run_id": "run_failed_skill",
            "runtime": "hermes",
            "model": "chat-test",
            "context": {"session_id": "panel_session_failed_skill"},
            "messages": [{"role": "user", "content": "load a missing skill"}],
            "system_context": {
                "version": version,
                "mode": mode,
                "digest": digest,
                "stable": stable,
                "turn": turn,
            },
        })
        events = [json.loads(line) async for line in response.content]
        completed = next(event for event in events if event["type"] == "activity_completed")
        assert completed["payload"] == {
            "call_id": "skill_failed",
            "name": "skill_view",
            "status": "failed",
            "error": {
                "code": "runtime_activity_failed",
                "message": "Skill 'missing-skill' not found.",
                "retryable": False,
            },
        }
        serialized = json.dumps(events)
        assert "private-skill-name" not in serialized
        assert "available_skills" not in serialized
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_bridge_blocks_unchanged_non_retryable_tool_retry():
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_guard",
        asyncio.get_running_loop(),
        queue,
        [{"name": "ultra_prompt_compile", "input_schema": {"type": "object"}}],
        10_000,
        "agent_guard",
    )
    decisions = []
    session.agent_ref[0] = SimpleNamespace(
        _set_tool_guardrail_halt=decisions.append,
        _runtime_checkpoint_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_first",
                "function": {"name": "ultra_prompt_compile", "arguments": "{}"},
            }],
        },
    )
    args = {"capability": "media.video.generate", "spec": {"intent": "ad"}}
    first = asyncio.create_task(asyncio.to_thread(
        session.invoke_platform_tool,
        "ultra_prompt_compile",
        args,
        "call_first",
    ))
    checkpoint = await queue.get()
    assert checkpoint["type"] == "checkpoint"
    request = await queue.get()
    assert request["type"] == "tool_request"
    assert session.submit_result({
        "call_id": "call_first",
        "ok": False,
        "error": {
            "code": "invalid_tool_arguments",
            "message": "spec.prompt is required",
            "retryable": False,
        },
    })
    first_result = json.loads(await first)
    assert first_result["error"]["code"] == "invalid_tool_arguments"
    assert decisions == []

    second_result = json.loads(session.invoke_platform_tool(
        "ultra_prompt_compile",
        args,
        "call_second",
    ))
    assert second_result["error"]["code"] == "repeated_non_retryable_tool_call"
    assert decisions[0].code == "repeated_non_retryable_tool_call"
    assert queue.empty()


@pytest.mark.asyncio
async def test_runtime_checkpoint_filters_local_activity_sibling_call():
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_checkpoint_filter",
        asyncio.get_running_loop(),
        queue,
        [{"name": "platform.prompt_compile", "input_schema": {"type": "object"}}],
        10_000,
        "agent_checkpoint_filter",
    )
    session.agent_ref[0] = SimpleNamespace(
        _runtime_checkpoint_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_skill",
                    "function": {"name": "skill_view", "arguments": '{"name":"media-qa"}'},
                },
                {
                    "id": "call_compile",
                    "function": {"name": "platform.prompt_compile", "arguments": "{}"},
                },
            ],
        },
    )
    call = asyncio.create_task(asyncio.to_thread(
        session.invoke_platform_tool,
        "platform.prompt_compile",
        {},
        "call_compile",
    ))
    checkpoint = await queue.get()
    assert checkpoint["type"] == "checkpoint"
    assert [item["id"] for item in checkpoint["payload"]["message"]["tool_calls"]] == ["call_compile"]
    assert (await queue.get())["type"] == "tool_request"
    assert session.submit_result({"call_id": "call_compile", "ok": True, "result": {"compiled": {}}})
    assert json.loads(await call) == {"compiled": {}}


@pytest.mark.asyncio
async def test_runtime_bridge_halts_immediately_on_terminal_platform_error():
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_terminal",
        asyncio.get_running_loop(),
        queue,
        [{"name": "ultra_quota_snapshot", "input_schema": {"type": "object"}}],
        10_000,
        "agent_terminal",
    )
    decisions = []
    session.agent_ref[0] = SimpleNamespace(
        _set_tool_guardrail_halt=decisions.append,
        _runtime_checkpoint_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_terminal",
                "function": {"name": "ultra_quota_snapshot", "arguments": "{}"},
            }],
        },
    )
    call = asyncio.create_task(asyncio.to_thread(
        session.invoke_platform_tool,
        "ultra_quota_snapshot",
        {},
        "call_terminal",
    ))
    assert (await queue.get())["type"] == "checkpoint"
    assert (await queue.get())["type"] == "tool_request"
    assert session.submit_result({
        "call_id": "call_terminal",
        "ok": False,
        "error": {
            "code": "tool_not_implemented",
            "message": "quota endpoint is not configured",
            "retryable": False,
        },
    })
    result = json.loads(await call)
    assert result["error"]["code"] == "tool_not_implemented"
    assert decisions[0].code == "terminal_platform_error"
    assert decisions[0].count == 1


def test_runtime_bridge_has_no_implicit_deadline_or_one_hour_cap():
    loop = asyncio.new_event_loop()
    try:
        unlimited = RuntimeBridgeSession("run_open", loop, asyncio.Queue(), [], 0, "agent_open")
        explicit = RuntimeBridgeSession("run_explicit", loop, asyncio.Queue(), [], 7_200_000, "agent_explicit")
        assert unlimited.deadline_seconds is None
        assert explicit.deadline_seconds == 7_200
    finally:
        loop.close()


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
