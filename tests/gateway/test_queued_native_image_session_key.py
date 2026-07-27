import base64
import importlib
import sys
import types
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6L2ioAAAAASUVORK5CYII="
)


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.typing = []
        self.processing_hooks = []

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

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.processing_hooks.append(("start", event.message_id, None))

    async def on_processing_complete(
        self,
        event: MessageEvent,
        outcome: ProcessingOutcome,
    ) -> None:
        self.processing_hooks.append(("complete", event.message_id, outcome))


class CaptureQueuedNativeImageAgent:
    calls = []

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls.append(message)
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class SequencedQueuedAgent(CaptureQueuedNativeImageAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls.append(message)
        call_number = len(type(self).calls)
        return {
            "final_response": f"done-{call_number}",
            "messages": [],
            "api_calls": 1,
            "interrupted": call_number == 2,
            "interrupt_message": "third" if call_number == 2 else None,
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
        chat_id="-1001",
        chat_type="group",
    )
    pending_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    adapter._pending_messages["agent:main:telegram:group:-1001"] = MessageEvent(
        text="describe this",
        message_type=MessageType.PHOTO,
        source=pending_source,
        media_urls=[str(image_path)],
        media_types=["image/png"],
        message_id="queued-1",
    )

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
    queued_message = CaptureQueuedNativeImageAgent.calls[1]
    assert isinstance(queued_message, list)
    assert queued_message[0]["type"] == "text"
    assert queued_message[0]["text"].startswith("describe this")
    assert any(part.get("type") == "image_url" for part in queued_message)
    assert adapter.processing_hooks == [
        ("start", "queued-1", None),
        ("complete", "queued-1", ProcessingOutcome.SUCCESS),
    ]


@pytest.mark.asyncio
async def test_queued_preprocessing_early_return_still_completes_lifecycle(monkeypatch, tmp_path):
    CaptureQueuedNativeImageAgent.calls = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedNativeImageAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    runner._prepare_inbound_message_text = AsyncMock(return_value=None)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm")
    session_key = "agent:main:telegram:dm:chat-1"
    adapter._pending_messages[session_key] = MessageEvent(
        text="@blocked",
        source=source,
        message_id="queued-blocked",
    )

    result = await runner._run_agent(
        message="first",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done-1"
    assert adapter.processing_hooks == [
        ("start", "queued-blocked", None),
        ("complete", "queued-blocked", ProcessingOutcome.SUCCESS),
    ]


@pytest.mark.asyncio
async def test_queued_preprocessing_exception_completes_failure(monkeypatch, tmp_path):
    CaptureQueuedNativeImageAgent.calls = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedNativeImageAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    runner._prepare_inbound_message_text = AsyncMock(side_effect=RuntimeError("prepare failed"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm")
    session_key = "agent:main:telegram:dm:chat-1"
    adapter._pending_messages[session_key] = MessageEvent(
        text="queued",
        source=source,
        message_id="queued-error",
    )

    with pytest.raises(RuntimeError, match="prepare failed"):
        await runner._run_agent(
            message="first",
            context_prompt="",
            history=[],
            source=source,
            session_id="session-1",
            session_key=session_key,
        )

    assert adapter.processing_hooks == [
        ("start", "queued-error", None),
        ("complete", "queued-error", ProcessingOutcome.FAILURE),
    ]


@pytest.mark.asyncio
async def test_nested_queued_turn_uses_its_own_cancelled_outcome(monkeypatch, tmp_path):
    SequencedQueuedAgent.calls = []
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = SequencedQueuedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    runner._queued_events = {}
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm")
    session_key = "agent:main:telegram:dm:chat-1"
    second = MessageEvent(text="second", source=source, message_id="queued-2")
    third = MessageEvent(text="third", source=source, message_id="queued-3")
    runner._enqueue_fifo(session_key, second, adapter)
    runner._enqueue_fifo(session_key, third, adapter)

    result = await runner._run_agent(
        message="first",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
        session_key=session_key,
    )

    assert result["final_response"] == "done-3"
    assert adapter.processing_hooks == [
        ("start", "queued-2", None),
        ("start", "queued-3", None),
        ("complete", "queued-3", ProcessingOutcome.SUCCESS),
        ("complete", "queued-2", ProcessingOutcome.CANCELLED),
    ]
