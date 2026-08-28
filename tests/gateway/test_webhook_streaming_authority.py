"""Webhook turns must never create token-streaming side effects."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.display_config import resolve_display_setting
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
)
from gateway.session import SessionSource
from gateway.session import build_session_key
from gateway.turn_context import TurnContext


class _FinalAgent:
    instances = []

    def __init__(self, **kwargs):
        self.model = kwargs["model"]
        self.session_id = kwargs["session_id"]
        self.tools = []
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0,
            context_length=200_000,
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.callback_seen_during_run = object()
        type(self).instances.append(self)

    def run_conversation(self, _message, **_kwargs):
        self.callback_seen_during_run = self.stream_delta_callback
        return {
            "final_response": "ordinary final",
            "completed": True,
            "messages": [],
            "api_calls": 1,
        }


class _StreamConsumerSpy:
    instances = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append((args, kwargs))

    def on_delta(self, _text):
        pass

    def finish(self, *_args):
        pass


class _LedgerOwnedWebhookProbe(BasePlatformAdapter):
    """Concrete webhook-shaped adapter that records Base's final send."""

    owns_final_delivery_ledger = True

    def __init__(self):
        config = PlatformConfig(enabled=True)
        config.typing_indicator = False
        super().__init__(config, Platform.WEBHOOK)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return SendResult(success=True, message_id="webhook-final")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _ProxyResponse:
    status = 200

    def __init__(self):
        self.content = self

    async def iter_any(self):
        yield b'data: {"choices":[{"delta":{"content":"proxy final"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


class _ProxySession:
    def __init__(self):
        self.body = None

    def post(self, _url, *, json, headers):
        self.body = json
        return _ProxyResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


def test_webhook_turn_forces_streaming_off_despite_both_enable_flags(monkeypatch):
    from gateway.run import TurnRunner
    import gateway.stream_consumer as stream_consumer_module

    _FinalAgent.instances.clear()
    _StreamConsumerSpy.instances.clear()
    monkeypatch.setattr(
        stream_consumer_module,
        "GatewayStreamConsumer",
        _StreamConsumerSpy,
    )

    user_config = {
        "display": {
            "platforms": {
                "webhook": {"streaming": True},
            },
        },
    }
    assert resolve_display_setting(user_config, "webhook", "streaming") is True

    gateway_runner = MagicMock()
    gateway_runner.config = SimpleNamespace(
        streaming=StreamingConfig(enabled=True, transport="edit")
    )
    gateway_runner._provider_routing = {}
    gateway_runner._agent_cache_lock = None
    gateway_runner._agent_cache = {}
    gateway_runner._session_db = None
    gateway_runner._prefill_messages = None
    gateway_runner._pending_model_notes = {}
    gateway_runner._pending_skills_reload_notes = {}
    gateway_runner.session_store._entries = {}
    gateway_runner._get_system_prompt_for_channel.return_value = None
    gateway_runner._resolve_session_agent_runtime.return_value = ("test-model", {})
    gateway_runner._resolve_session_reasoning_config.return_value = None
    gateway_runner._resolve_session_service_tier.return_value = None
    gateway_runner._resolve_turn_agent_config.return_value = {
        "model": "test-model",
        "runtime": {},
    }
    gateway_runner._agent_config_signature.return_value = ("test-signature",)
    gateway_runner._extract_cache_busting_config.return_value = {}
    gateway_runner._refresh_fallback_model.return_value = None
    gateway_runner._consume_pending_native_image_paths.return_value = []
    gateway_runner._consume_pending_turn_sidecar_notes.return_value = []
    gateway_runner._is_telegram_topic_lane.return_value = False
    gateway_runner._is_discord_auto_thread_lane.return_value = False
    gateway_runner._is_relay_discord_channel_lane.return_value = False
    gateway_runner._adapter_for_source.return_value = object()
    gateway_runner._build_stream_consumer_config.return_value = (
        SimpleNamespace(),
        False,
    )

    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="authority-target",
        user_id="webhook-source",
    )
    ctx = TurnContext(
        source=source,
        message="execute webhook operation",
        history=[],
        session_id="webhook-session",
        session_key="webhook-session-key",
        user_config=user_config,
        AIAgent=_FinalAgent,
        resolve_display_setting=resolve_display_setting,
        _run_still_current=lambda: True,
        _hooks_ref=SimpleNamespace(loaded_hooks=False),
        interim_assistant_messages_enabled=False,
    )

    result = TurnRunner(gateway_runner, ctx).run_sync()

    assert result["final_response"] == "ordinary final"
    assert result["completed"] is True
    assert len(_FinalAgent.instances) == 1
    assert _FinalAgent.instances[0].callback_seen_during_run is None
    assert _FinalAgent.instances[0].stream_delta_callback is None
    assert ctx.stream_consumer_holder == [None]
    assert _StreamConsumerSpy.instances == []
    gateway_runner._build_stream_consumer_config.assert_not_called()


@pytest.mark.asyncio
async def test_proxy_webhook_keeps_final_in_base_owned_delivery(monkeypatch):
    """Defense in depth for a proxy path blocked by today's grant carrier."""

    from gateway.run import GatewayRunner
    import gateway.stream_consumer as stream_consumer_module

    _StreamConsumerSpy.instances.clear()
    monkeypatch.setattr(
        stream_consumer_module,
        "GatewayStreamConsumer",
        _StreamConsumerSpy,
    )
    monkeypatch.delenv("GATEWAY_PROXY_KEY", raising=False)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {
            "display": {
                "platforms": {
                    "webhook": {"streaming": True},
                },
            },
        },
    )

    proxy_session = _ProxySession()
    monkeypatch.setattr("aiohttp.ClientSession", lambda **_kwargs: proxy_session)
    monkeypatch.setattr("aiohttp.ClientTimeout", lambda **_kwargs: object())

    adapter = _LedgerOwnedWebhookProbe()
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:default:deploy:generic:proxy-proof",
        chat_name="webhook/deploy",
        chat_type="webhook",
        user_id="webhook:deploy",
        user_name="deploy",
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner.config = SimpleNamespace(
        streaming=StreamingConfig(enabled=True, transport="edit")
    )
    runner._get_proxy_url = lambda: "http://proxy.invalid:8642"
    runner._adapter_for_source = lambda _source: adapter
    runner._thread_metadata_for_source = lambda _source, _message_id: None
    runner._build_stream_consumer_config = MagicMock(
        side_effect=AssertionError("webhook proxy constructed a stream consumer")
    )

    observed_result = None

    async def proxy_handler(_event):
        nonlocal observed_result
        observed_result = await runner._run_agent_via_proxy(
            message="deploy",
            context_prompt="",
            history=[],
            source=source,
            session_id="proxy-session",
        )
        return observed_result["final_response"]

    adapter.set_message_handler(proxy_handler)
    event = MessageEvent(text="deploy", source=source, message_id="delivery-1")
    await adapter._process_message_background(event, build_session_key(source))

    assert proxy_session.body["stream"] is True
    assert observed_result["final_response"] == "proxy final"
    assert observed_result["response_previewed"] is False
    assert "already_sent" not in observed_result
    assert _StreamConsumerSpy.instances == []
    runner._build_stream_consumer_config.assert_not_called()
    assert adapter.sent == [
        {
            "chat_id": source.chat_id,
            "content": "proxy final",
            "reply_to": "delivery-1",
            "metadata": {"notify": True},
        }
    ]
