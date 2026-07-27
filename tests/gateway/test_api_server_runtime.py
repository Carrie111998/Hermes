from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from types import SimpleNamespace

import pytest
import gateway.api_server_runtime as runtime_module
from gateway.api_server_audit import request_audit_middleware

from gateway.api_server_runtime import (
    APIServerRuntimeMixin,
    RuntimeBridgeSession,
    _pin_run_model,
    _resume_runtime_history,
    _runtime_attachment_parts,
    _runtime_tool_middleware,
)

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class _RuntimeAdapter(APIServerRuntimeMixin):
    def _check_auth(self, _request):
        return None

    async def _run_agent_bridge(self, **kwargs):
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
                {
                    "type": "function",
                    "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}},
                },
                {
                    "type": "function",
                    "function": {"name": "web_extract", "description": "", "parameters": {"type": "object"}},
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
        assert agent.valid_tool_names == {
            "ask_user_question",
            "skill_view",
            "ultra_media_job_create",
            "web_extract",
            "web_search",
        }
        ask_schema = next(
            tool["function"]["parameters"]
            for tool in agent.tools
            if tool["function"]["name"] == "ask_user_question"
        )
        option_schema = (
            ask_schema["properties"]["questions"]["items"]
            ["properties"]["options"]["items"]
        )
        assert option_schema["required"] == ["label", "value"]
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
        skill_envelope = json.dumps({"success": True, "content": "workflow instructions"})
        skill_result = _runtime_tool_middleware(
            tool_name="skill_view",
            args={"name": "media-qa"},
            session_id=kwargs["session_id"],
            tool_call_id="skill_call",
            next_call=lambda _args: skill_envelope,
        )
        assert skill_result == skill_envelope
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


def test_runtime_attachment_parts_preserve_image_pixels_and_require_video_frames():
    image = base64.b64encode(b"png-bytes").decode()
    parts = _runtime_attachment_parts([{
        "role": "product_photo",
        "asset_id": "asset_image",
        "filename": "product.png",
        "media_type": "image",
        "mime_type": "image/png",
        "data": image,
    }])
    assert parts == [{
        "type": "text",
        "text": "[Attached image: product.png; role=product_photo; asset_id=asset_image]",
    }, {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{image}"},
    }]

    with pytest.raises(ValueError, match="representative image frame"):
        _runtime_attachment_parts([{
            "role": "user_upload",
            "asset_id": "asset_video",
            "filename": "clip.mp4",
            "media_type": "video",
            "mime_type": "video/mp4",
            "data": base64.b64encode(b"video-bytes").decode(),
        }])


@pytest.mark.asyncio
async def test_runtime_bridge_delivers_image_attachment_as_multimodal_user_content():
    captured = {}

    class AttachmentAdapter(APIServerRuntimeMixin):
        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            captured["user_message"] = kwargs["user_message"]
            return {"final_response": "seen"}, {"total_tokens": 1}

    adapter = AttachmentAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        stable = "platform rules"
        version = "attachments/v1"
        turn = '{"attachment_asset_ids":{"user_upload":["asset_image"]}}'
        digest = "sha256:" + hashlib.sha256(
            f"{version}\nreplace\n{stable}\n{turn}".encode(),
        ).hexdigest()
        encoded = base64.b64encode(b"png-bytes").decode()
        response = await client.post("/v1/runtime/runs", json={
            "run_id": "run_attachment",
            "model": "chat-test",
            "messages": [{"role": "user", "content": "describe it"}],
            "system_context": {
                "version": version,
                "mode": "replace",
                "stable": stable,
                "turn": turn,
                "digest": digest,
            },
            "attachments": [{
                "role": "user_upload",
                "asset_id": "asset_image",
                "filename": "reference.png",
                "media_type": "image",
                "mime_type": "image/png",
                "data": encoded,
            }],
        })
        assert response.status == 200
        events = [json.loads(line) async for line in response.content]
        assert events[-1]["type"] == "completed"
        content = captured["user_message"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "describe it"}
        assert content[-1]["image_url"]["url"] == f"data:image/png;base64,{encoded}"
    finally:
        await client.close()


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
                "requires_skill_guidance": True,
            }, {
                "name": "ask_user_question",
                "description": "ask one structured question",
                "input_schema": {
                    "type": "object",
                    "required": ["questions"],
                    "properties": {
                        "questions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "options": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["label", "value"],
                                            "properties": {
                                                "label": {"type": "string"},
                                                "value": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
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
                "arguments": {
                    "digest": "sha256:" + hashlib.sha256(
                        b"workflow instructions",
                    ).hexdigest(),
                },
            },
        }
        checkpoint = json.loads(await response.content.readline())
        assert checkpoint["type"] == "checkpoint"
        assert checkpoint["payload"]["message"]["tool_calls"][0]["id"] == "call_01"
        tool_request = json.loads(await response.content.readline())
        assert tool_request["type"] == "tool_request"
        expected_proof = {
            "name": "media-qa",
            "digest": "sha256:" + hashlib.sha256(
                b"workflow instructions",
            ).hexdigest(),
        }
        assert tool_request["payload"]["skill"] == expected_proof
        assert tool_request["payload"]["skills"] == [expected_proof]

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

        async def _run_agent_bridge(self, **kwargs):
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
        async def _run_agent_bridge(self, **kwargs):
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
    assert first_result["error"]["recovery"] == {
        "action": "correct_arguments",
        "remaining_attempts": 1,
        "same_arguments_allowed": False,
    }
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
async def test_runtime_bridge_allows_one_corrected_argument_attempt_then_halts():
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_correction",
        asyncio.get_running_loop(),
        queue,
        [{"name": "ask_user_question", "input_schema": {"type": "object"}}],
        10_000,
        "agent_correction",
    )
    decisions = []
    agent = SimpleNamespace(_set_tool_guardrail_halt=decisions.append)
    session.agent_ref[0] = agent

    async def invoke(call_id, args, result):
        agent._runtime_checkpoint_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "function": {
                    "name": "ask_user_question",
                    "arguments": json.dumps(args),
                },
            }],
        }
        pending = asyncio.create_task(asyncio.to_thread(
            session.invoke_platform_tool,
            "ask_user_question",
            args,
            call_id,
        ))
        assert (await queue.get())["type"] == "checkpoint"
        assert (await queue.get())["type"] == "tool_request"
        assert session.submit_result({"call_id": call_id, **result})
        return json.loads(await pending)

    first = await invoke(
        "call_invalid",
        {"questions": [{"options": [{"label": "A"}]}]},
        {
            "ok": False,
            "error": {
                "code": "invalid_tool_arguments",
                "message": "options[0].value is required",
                "retryable": False,
            },
        },
    )
    assert first["error"]["recovery"]["action"] == "correct_arguments"

    exhausted = await invoke(
        "call_still_invalid",
        {"questions": [{"options": [{"label": "A", "description": "retry"}]}]},
        {
            "ok": False,
            "error": {
                "code": "invalid_tool_arguments",
                "message": "options[0].value is required",
                "retryable": False,
            },
        },
    )
    assert exhausted["error"]["code"] == "argument_correction_exhausted"
    assert exhausted["error"]["cause"]["code"] == "invalid_tool_arguments"
    assert decisions[0].code == "argument_correction_exhausted"
    assert decisions[0].count == 2


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


def test_runtime_bridge_deadline_attribute_tracks_request():
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


def _run_body(run_id: str, **extra):
    stable = "platform rules"
    version = "bridge-test/v1"
    digest = "sha256:" + hashlib.sha256(
        f"{version}\nreplace\n{stable}\n".encode("utf-8"),
    ).hexdigest()
    body = {
        "run_id": run_id,
        "model": "chat-test",
        "messages": [{"role": "user", "content": "go"}],
        "system_context": {
            "version": version,
            "mode": "replace",
            "stable": stable,
            "turn": "",
            "digest": digest,
        },
    }
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_runtime_run_over_limit_returns_retryable_429(monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_MAX_CONCURRENT", "1")
    release = asyncio.Event()

    class BlockingAdapter(APIServerRuntimeMixin):
        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            agent = SimpleNamespace(tools=[], valid_tool_names=set(), model="configured-model")
            kwargs["agent_configurator"](agent)
            await release.wait()
            return {"final_response": "done"}, {}

    adapter = BlockingAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        first = await client.post("/v1/runtime/runs", json=_run_body("run_gate_a"))
        assert first.status == 200
        assert json.loads(await first.content.readline())["type"] == "run_started"

        second = await client.post("/v1/runtime/runs", json=_run_body("run_gate_b"))
        assert second.status == 429
        payload = await second.json()
        assert payload["error"]["code"] == "runtime_concurrency_exceeded"
        assert payload["error"]["retryable"] is True

        release.set()
        events = [json.loads(line) async for line in first.content]
        assert events[-1]["type"] == "completed"

        active = -1
        for _ in range(200):
            with runtime_module._RUNTIME_GATE_LOCK:
                active = runtime_module._ACTIVE_RUN_COUNT
            if active == 0:
                break
            await asyncio.sleep(0.01)
        assert active == 0
    finally:
        release.set()
        await client.close()


@pytest.mark.asyncio
async def test_runtime_interrupt_waits_without_thread_pool(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("interrupt path must not use asyncio.to_thread")

    monkeypatch.setattr(asyncio, "to_thread", _forbidden)

    class InterruptAdapter(APIServerRuntimeMixin):
        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            stop = asyncio.Event()
            agent = SimpleNamespace(
                tools=[],
                valid_tool_names=set(),
                model="configured-model",
                interrupt=lambda reason: stop.set(),
            )
            kwargs["agent_configurator"](agent)
            await stop.wait()
            return {"final_response": "stopped"}, {}

    adapter = InterruptAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    app.router.add_post("/v1/runtime/runs/{run_id}/interrupt", adapter._handle_runtime_interrupt)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        run_response = await client.post("/v1/runtime/runs", json=_run_body("run_interrupt_async"))
        assert run_response.status == 200
        assert json.loads(await run_response.content.readline())["type"] == "run_started"

        interrupt_response = await client.post(
            "/v1/runtime/runs/run_interrupt_async/interrupt",
            json={"reason": "user cancelled"},
        )
        assert interrupt_response.status == 204

        events = [json.loads(line) async for line in run_response.content]
        assert events[-1]["type"] == "completed"
        assert events[-1]["payload"]["text"] == "stopped"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_disconnect_interrupts_run_and_clears_session():
    class DisconnectAdapter(APIServerRuntimeMixin):
        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            stop = asyncio.Event()
            agent = SimpleNamespace(
                tools=[],
                valid_tool_names=set(),
                model="configured-model",
                interrupt=lambda reason: stop.set(),
            )
            kwargs["agent_configurator"](agent)
            for index in range(2000):
                if stop.is_set():
                    break
                kwargs["stream_delta_callback"](f"delta-{index}")
                await asyncio.sleep(0.01)
            assert stop.is_set(), "pump never interrupted the run after disconnect"
            return {"final_response": "stopped"}, {}

    adapter = DisconnectAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        run_response = await client.post("/v1/runtime/runs", json=_run_body("run_disconnect"))
        assert run_response.status == 200
        assert json.loads(await run_response.content.readline())["type"] == "run_started"
        with runtime_module._SESSIONS_LOCK:
            session = runtime_module._SESSIONS.get("run_disconnect")
        assert session is not None

        run_response.close()

        for _ in range(1000):
            with runtime_module._SESSIONS_LOCK:
                cleared = "run_disconnect" not in runtime_module._SESSIONS
            if cleared and session.finished.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("run kept going after orchestrator disconnect")
        assert session.interrupted.is_set()
    finally:
        await client.close()


def test_resume_history_projects_externalized_output_ref():
    history = _resume_runtime_history(
        [{"role": "user", "content": "make an image"}],
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_ext",
                    "type": "function",
                    "function": {"name": "media.generate_image", "arguments": "{}"},
                }],
            },
        },
        [{
            "tool_call_id": "call_ext",
            "status": "succeeded",
            "output_ref": {"asset_id": "asset_01", "kind": "media_batch"},
        }],
    )
    assert json.loads(history[-1]["content"]) == {
        "status": "externalized",
        "output_ref": {"asset_id": "asset_01", "kind": "media_batch"},
    }


def test_resume_history_prefers_inline_output_over_output_ref():
    history = _resume_runtime_history(
        [{"role": "user", "content": "make an image"}],
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_inline",
                    "type": "function",
                    "function": {"name": "media.generate_image", "arguments": "{}"},
                }],
            },
        },
        [{
            "tool_call_id": "call_inline",
            "status": "succeeded",
            "output": {"url": "asset://image/1"},
            "output_ref": {"asset_id": "asset_01"},
        }],
    )
    assert json.loads(history[-1]["content"]) == {"url": "asset://image/1"}


@pytest.mark.asyncio
async def test_unbounded_deadline_tool_wait_is_capped(monkeypatch):
    assert runtime_module._UNBOUNDED_TOOL_WAIT_CAP_SECONDS == 3600.0
    monkeypatch.setattr(runtime_module, "_UNBOUNDED_TOOL_WAIT_CAP_SECONDS", 0.05)
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_cap",
        asyncio.get_running_loop(),
        queue,
        [{"name": "ultra_media_job_create", "input_schema": {"type": "object"}}],
        0,
        "agent_cap",
    )
    session.agent_ref[0] = SimpleNamespace(
        interrupt=lambda reason: None,
        _runtime_checkpoint_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_cap",
                "function": {"name": "ultra_media_job_create", "arguments": "{}"},
            }],
        },
    )
    assert session.deadline_seconds is None
    result = json.loads(await asyncio.to_thread(
        session.invoke_platform_tool,
        "ultra_media_job_create",
        {},
        "call_cap",
    ))
    assert result["error"]["code"] == "runtime_deadline_exceeded"
    assert session.pending == {}


@pytest.mark.asyncio
async def test_interrupt_wakeup_reports_run_interrupted_not_deadline():
    queue = asyncio.Queue()
    session = RuntimeBridgeSession(
        "run_intr_attr",
        asyncio.get_running_loop(),
        queue,
        [{"name": "ultra_media_job_create", "input_schema": {"type": "object"}}],
        10_000,
        "agent_intr_attr",
    )
    session.agent_ref[0] = SimpleNamespace(
        interrupt=lambda reason: None,
        _runtime_checkpoint_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_intr",
                "function": {"name": "ultra_media_job_create", "arguments": "{}"},
            }],
        },
    )
    call = asyncio.create_task(asyncio.to_thread(
        session.invoke_platform_tool,
        "ultra_media_job_create",
        {},
        "call_intr",
    ))
    assert (await queue.get())["type"] == "checkpoint"
    assert (await queue.get())["type"] == "tool_request"
    session.interrupt("orchestrator stream disconnected")
    result = json.loads(await call)
    assert result["error"]["code"] == "run_interrupted"
    assert result["error"]["message"] == "run was interrupted"


def test_sweeper_evicts_only_stale_finished_sessions():
    loop = asyncio.new_event_loop()
    try:
        stale = RuntimeBridgeSession("run_stale", loop, asyncio.Queue(), [], 0, "agent_stale")
        stale.finished.set()
        stale.finished_at = time.monotonic() - 10 * runtime_module._FINISHED_SESSION_TTL_SECONDS
        fresh = RuntimeBridgeSession("run_fresh", loop, asyncio.Queue(), [], 0, "agent_fresh")
        fresh.finished.set()
        fresh.finished_at = time.monotonic()
        live = RuntimeBridgeSession("run_live", loop, asyncio.Queue(), [], 0, "agent_live")
        with runtime_module._SESSIONS_LOCK:
            runtime_module._SESSIONS.update({
                "run_stale": stale,
                "run_fresh": fresh,
                "run_live": live,
            })
        removed = runtime_module._sweep_finished_sessions()
        assert removed == ["run_stale"]
        with runtime_module._SESSIONS_LOCK:
            assert "run_stale" not in runtime_module._SESSIONS
            assert "run_fresh" in runtime_module._SESSIONS
            assert "run_live" in runtime_module._SESSIONS
    finally:
        with runtime_module._SESSIONS_LOCK:
            for key in ("run_stale", "run_fresh", "run_live"):
                runtime_module._SESSIONS.pop(key, None)
        loop.close()


@pytest.mark.asyncio
async def test_runtime_run_pins_one_hour_prompt_cache_ttl():
    class TTLAdapter(APIServerRuntimeMixin):
        _api_key = ""

        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            # agent_init defaults _cache_ttl to "5m"; runtime bridge runs
            # park past that tier, so the configurator must pin "1h".
            agent = SimpleNamespace(
                tools=[], valid_tool_names=set(), model="configured-model", _cache_ttl="5m",
            )
            kwargs["agent_configurator"](agent)
            assert agent._cache_ttl == "1h"
            return {"final_response": "done"}, {"total_tokens": 1}

    adapter = TTLAdapter()
    app = web.Application()
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        stable = "platform rules"
        version = "ttl/v1"
        digest = "sha256:" + hashlib.sha256(
            f"{version}\nreplace\n{stable}\n".encode(),
        ).hexdigest()
        response = await client.post("/v1/runtime/runs", json={
            "run_id": "run_ttl",
            "model": "chat-test",
            "messages": [{"role": "user", "content": "make an image"}],
            "system_context": {
                "version": version,
                "mode": "replace",
                "stable": stable,
                "turn": "",
                "digest": digest,
            },
        })
        assert response.status == 200
        events = [json.loads(line) async for line in response.content]
        assert events[-1]["type"] == "completed"
        assert events[-1]["payload"]["text"] == "done"
    finally:
        await client.close()


def _audit_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "gateway.api_server.audit"
    ]


class _AuditRunAdapter(APIServerRuntimeMixin):
    def _check_auth(self, _request):
        return None

    async def _run_agent_bridge(self, **kwargs):
        agent = SimpleNamespace(tools=[], valid_tool_names=set(), model="configured-model")
        kwargs["agent_configurator"](agent)
        return {"final_response": "done"}, {"total_tokens": 1}


def _audited_app(adapter: APIServerRuntimeMixin) -> web.Application:
    app = web.Application(middlewares=[request_audit_middleware])
    app.router.add_post("/v1/runtime/runs", adapter._handle_runtime_run)
    app.router.add_post("/v1/runtime/runs/{run_id}/tool-results", adapter._handle_runtime_tool_result)
    app.router.add_post("/v1/runtime/runs/{run_id}/interrupt", adapter._handle_runtime_interrupt)
    return app


@pytest.mark.asyncio
async def test_runtime_run_audit_completion_line_carries_run_id(caplog):
    caplog.set_level(logging.INFO, logger="gateway.api_server.audit")
    client = TestClient(TestServer(_audited_app(_AuditRunAdapter())))
    await client.start_server()
    try:
        response = await client.post("/v1/runtime/runs", json=_run_body("run_audit_run"))
        assert response.status == 200
        events = [json.loads(line) async for line in response.content]
        assert events[-1]["type"] == "completed"
    finally:
        await client.close()

    completion_lines = [
        line for line in _audit_messages(caplog)
        if "'action': 'api.request'" in line and "'result': 'completed'" in line
    ]
    assert completion_lines, _audit_messages(caplog)
    assert "'run_id': 'run_audit_run'" in completion_lines[-1]
    # The entry line is logged before the body is parsed; run attribution
    # lives on the completion line only.
    started_lines = [
        line for line in _audit_messages(caplog) if "'result': 'started'" in line
    ]
    assert started_lines
    assert "'run_id'" not in started_lines[0]


@pytest.mark.asyncio
async def test_runtime_tool_result_audit_line_carries_run_id(caplog):
    caplog.set_level(logging.INFO, logger="gateway.api_server.audit")
    client = TestClient(TestServer(_audited_app(_AuditRunAdapter())))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/runtime/runs/run_audit_tool/tool-results",
            json={"call_id": "call_x", "ok": True, "result": {}},
        )
        # The run is not active: the handler still stamps the path run_id
        # onto the request before the session lookup, so even the 404 is
        # attributable in the audit trail.
        assert response.status == 404
    finally:
        await client.close()

    lines = [
        line for line in _audit_messages(caplog)
        if "'action': 'api.request'" in line and "'status': 404" in line
    ]
    assert lines, _audit_messages(caplog)
    assert "'run_id': 'run_audit_tool'" in lines[-1]


@pytest.mark.asyncio
async def test_runtime_interrupt_audit_line_carries_run_id(caplog):
    caplog.set_level(logging.INFO, logger="gateway.api_server.audit")

    class InterruptAdapter(APIServerRuntimeMixin):
        def _check_auth(self, _request):
            return None

        async def _run_agent_bridge(self, **kwargs):
            stop = asyncio.Event()
            agent = SimpleNamespace(
                tools=[],
                valid_tool_names=set(),
                model="configured-model",
                interrupt=lambda reason: stop.set(),
            )
            kwargs["agent_configurator"](agent)
            await stop.wait()
            return {"final_response": "stopped"}, {}

    client = TestClient(TestServer(_audited_app(InterruptAdapter())))
    await client.start_server()
    try:
        run_response = await client.post("/v1/runtime/runs", json=_run_body("run_audit_intr"))
        assert run_response.status == 200
        assert json.loads(await run_response.content.readline())["type"] == "run_started"

        interrupt_response = await client.post(
            "/v1/runtime/runs/run_audit_intr/interrupt",
            json={"reason": "audit test"},
        )
        assert interrupt_response.status == 204

        events = [json.loads(line) async for line in run_response.content]
        assert events[-1]["type"] == "completed"
    finally:
        await client.close()

    interrupt_lines = [
        line for line in _audit_messages(caplog)
        if "/v1/runtime/runs/run_audit_intr/interrupt" in line
        and "'result': 'completed'" in line
    ]
    assert interrupt_lines, _audit_messages(caplog)
    assert "'run_id': 'run_audit_intr'" in interrupt_lines[-1]
