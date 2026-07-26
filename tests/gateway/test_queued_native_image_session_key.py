import base64
import importlib
import json
import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6L2ioAAAAASUVORK5CYII="
)


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.typing = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class CaptureQueuedNativeImageAgent:
    calls = []
    authenticated_gateway_tool_dispatch_version = 1

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(
        self,
        message,
        conversation_history=None,
        task_id=None,
        authenticated_gateway_context=None,
    ):
        type(self).calls.append(
            {
                "message": message,
                "authenticated": authenticated_gateway_context is not None,
                "message_id": getattr(authenticated_gateway_context, "message_id", None),
            }
        )
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class ProtectedDispatchE2EAgent:
    authenticated_gateway_tool_dispatch_version = 1
    registry: Any = None
    captured_context = None

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(
        self,
        message,
        conversation_history=None,
        task_id=None,
        authenticated_gateway_context=None,
    ):
        type(self).captured_context = authenticated_gateway_context
        dispatch_result = type(self).registry.dispatch(
            "e2e-protected",
            {"value": message},
            authenticated_gateway_context=authenticated_gateway_context,
        )
        return {
            "final_response": dispatch_result,
            "messages": [],
            "history_offset": 0,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_kw: "native"
    return runner


@pytest.mark.asyncio
async def test_queued_followup_uses_pending_event_session_key_for_native_images(monkeypatch, tmp_path):
    CaptureQueuedNativeImageAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedNativeImageAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)

    image_path = tmp_path / "queued-image.png"
    image_path.write_bytes(_ONE_BY_ONE_PNG)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="-1001",
        chat_type="group",
    )
    pending_source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    pending_event = MessageEvent(
        text="describe this",
        message_type=MessageType.PHOTO,
        source=pending_source,
        media_urls=[str(image_path)],
        media_types=["image/png"],
        message_id="queued-1",
    )
    # Simulate the host-owned post-authorization decision captured when this
    # event entered the busy-session queue. Adapter-controlled source shape
    # alone must never grant protected authority.
    pending_event._authenticated_gateway_request = True
    adapter._pending_messages["agent:main:telegram:group:-1001"] = pending_event

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-native-image-followup",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done-2"
    assert len(CaptureQueuedNativeImageAgent.calls) == 2
    assert CaptureQueuedNativeImageAgent.calls[0]["authenticated"] is False
    assert CaptureQueuedNativeImageAgent.calls[1]["authenticated"] is True
    assert CaptureQueuedNativeImageAgent.calls[1]["message_id"] == "queued-1"
    queued_message = CaptureQueuedNativeImageAgent.calls[1]["message"]
    assert isinstance(queued_message, list)
    assert queued_message[0]["type"] == "text"
    assert queued_message[0]["text"].startswith("describe this")
    assert any(part.get("type") == "image_url" for part in queued_message)


@pytest.mark.asyncio
async def test_gateway_run_issues_exact_live_context_for_real_registry_dispatch(
    monkeypatch,
    tmp_path,
):
    from tools.registry import ToolRegistry

    seen = []
    registry = ToolRegistry()
    registry.register(
        name="e2e-protected",
        toolset="test",
        schema={
            "name": "e2e-protected",
            "description": "E2E protected dispatch",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
        handler=lambda args, *, tool_context: seen.append(
            (
                args["value"],
                tool_context.platform,
                tool_context.user_id,
                tool_context.chat_id,
                tool_context.message_id,
            )
        ) or "protected-ok",
        requires_authenticated_gateway=True,
    )
    ProtectedDispatchE2EAgent.registry = registry
    ProtectedDispatchE2EAgent.captured_context = None

    fake_dotenv = types.ModuleType("dotenv")
    setattr(fake_dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    setattr(fake_run_agent, "AIAgent", ProtectedDispatchE2EAgent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***"},
    )

    runner = _make_runner(CaptureAdapter())
    source = SessionSource(
        platform=Platform.TELEGRAM,
        user_id="user-1",
        chat_id="chat-1",
        chat_type="dm",
    )

    result = await runner._run_agent(
        message="run protected",
        context_prompt="",
        history=[],
        source=source,
        session_id="e2e-authenticated-dispatch",
        session_key="agent:main:telegram:dm:chat-1",
        event_message_id="exact-message-1",
        authenticated_gateway_request=True,
    )

    assert result["final_response"] == "protected-ok"
    assert seen == [
        (
            "run protected",
            "telegram",
            "user-1",
            "chat-1",
            "exact-message-1",
        )
    ]

    stale_result = registry.dispatch(
        "e2e-protected",
        {"value": "stale"},
        authenticated_gateway_context=ProtectedDispatchE2EAgent.captured_context,
    )
    assert isinstance(stale_result, str)
    denied = json.loads(stale_result)
    assert denied["error_type"] == "authenticated_gateway_required"
