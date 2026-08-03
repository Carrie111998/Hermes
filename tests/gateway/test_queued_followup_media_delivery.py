"""Queued-follow-up first-response delivery must stay media-aware.

When a message is queued (``/queue`` or an interrupt follow-up) behind a
turn that is still running, ``GatewayRunner._run_agent_inner`` flushes the
just-finished turn's response directly via ``adapter.send()`` before
recursing into the queued turn — bypassing the extraction/upload stage that
every other delivery path (streaming post-processing, the non-streaming
``_process_message_background`` pipeline) runs on a response before it
reaches the user. A response containing an explicit ``MEDIA:<path>``
directive therefore leaked the raw tag as chat text instead of stripping it
and uploading the attachment.

The fix routes this flush through the same ``_deliver_media_attachments``
helper used by the post-stream rescan (``GatewayRunner._deliver_media_from_response``,
covered by ``test_post_stream_media_delivery.py`` /
``test_73771_media_resend_dedup.py`` / ``test_tts_media_routing.py``), so the
"already streamed" queued-flush branch — text reached the user via
streaming, but streaming never uploads attachments on its own — is fixed by
the same change and exercised by that existing shared-helper coverage.
"""

import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.images_sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id=f"sent-{len(self.sent)}")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
        self.images_sent.append({"chat_id": chat_id, "images": images, "metadata": metadata})
        return SendResult(success=True, message_id="imgs")


class CaptureQueuedFollowupAgent:
    """Fake AIAgent: first call returns a MEDIA-tagged reply, second is plain."""

    calls = []
    first_response = ""

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls.append(message)
        if len(type(self).calls) == 1:
            return {
                "final_response": type(self).first_response,
                "messages": [],
                "api_calls": 1,
            }
        return {
            "final_response": "second turn done",
            "messages": [],
            "api_calls": 1,
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


def _allowed_media_path(tmp_path, monkeypatch, name):
    root = tmp_path / "media-cache"
    media_file = root / name
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"media")
    monkeypatch.setattr(
        "gateway.platforms.base.MEDIA_DELIVERY_SAFE_ROOTS",
        (root,),
    )
    return media_file.resolve()


@pytest.mark.asyncio
async def test_queued_followup_first_response_media_not_leaked_and_uploaded(monkeypatch, tmp_path):
    """Reproduces the reported bug: MEDIA:<path> in a queued first response
    used to reach the user as literal text, with no upload attempted."""
    media_file = _allowed_media_path(tmp_path, monkeypatch, "chart.png")
    first_response = f"Here is the chart.\nMEDIA:{media_file}"

    CaptureQueuedFollowupAgent.calls = []
    CaptureQueuedFollowupAgent.first_response = first_response

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedFollowupAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="group",
        thread_id="topic-42",
    )
    session_key = "agent:main:telegram:group:chat-1"

    # A message queued (e.g. via /queue) behind the still-running turn that
    # will produce ``first_response`` above.
    adapter._pending_messages[session_key] = MessageEvent(
        text="what's next",
        message_type=MessageType.TEXT,
        source=source,
        message_id="queued-1",
    )

    result = await runner._run_agent(
        message="generate the chart",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-queued-media",
        session_key=session_key,
    )

    # The queued follow-up still ran as the second turn.
    assert len(CaptureQueuedFollowupAgent.calls) == 2
    assert result["final_response"] == "second turn done"

    # Text must be sent exactly once, and must never carry the raw
    # directive — the model's internal attachment contract is not
    # user-facing text.
    texts = [entry["content"] for entry in adapter.sent]
    assert not any("MEDIA:" in text for text in texts)
    assert texts.count("Here is the chart.") == 1

    # The file must actually be uploaded natively, not silently dropped.
    assert len(adapter.images_sent) == 1
    delivery = adapter.images_sent[0]
    assert delivery["chat_id"] == "chat-1"
    assert str(media_file) in delivery["images"][0][0]

    # Both lanes must carry the same intended thread routing.
    text_send = next(e for e in adapter.sent if e["content"] == "Here is the chart.")
    assert text_send["metadata"] == delivery["metadata"]
    assert delivery["metadata"].get("thread_id") == "topic-42"
