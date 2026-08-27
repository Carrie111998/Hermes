from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    _SAFE_OUTBOUND_POLICY_NOTICE,
    _outbound_gate_required_for_target,
    bind_outbound_receipt_context,
    extract_user_visible_strings,
    scan_terminal_transport_inventory,
)
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CapabilityDescriptor
from hermes_cli import plugins as plugins_mod
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture(autouse=True)
def _default_trusted_final_policy(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "allow"},
    )


class GateAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.TELEGRAM)
        self.sent = []
        self.edited = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="m1")

    async def edit_message(
        self, chat_id, message_id, content, *, finalize=False, metadata=None
    ):
        self.edited.append((chat_id, message_id, content, finalize))
        return SendResult(success=True, message_id=message_id)


class AllEgressAdapter(GateAdapter):
    def __init__(self):
        super().__init__()
        self.egress = []
        self._platform_by_chat = {"paul": "slack"}

    async def send_for_platform(self, logical_platform, chat_id, content, metadata=None):
        self.egress.append(("relay", str(logical_platform), chat_id, content))
        return SendResult(success=True)

    async def send_draft(self, chat_id, draft_id, content, metadata=None):
        self.egress.append(("draft", chat_id, draft_id, content))
        return SendResult(success=True)

    async def send_native_task_card_progress(
        self, chat_id, tasks, *, title="Working", metadata=None, fallback_text=None
    ):
        self.egress.append(("card", chat_id, tasks, title, fallback_text))
        return SendResult(success=True)

    async def _post_interactive(self, chat_id, interactive_body, reply_to=None, metadata=None):
        self.egress.append(("interactive", chat_id, interactive_body))
        return SendResult(success=True)

    async def _send_media(
        self, chat_id, *, source, caption=None, metadata=None, **kwargs
    ):
        self.egress.append(("media", chat_id, source, caption))
        return SendResult(success=True)


class NativeStreamAdapter(GateAdapter):
    def __init__(self):
        super().__init__()
        self.streamed = []

    async def send_stream_frame(
        self, text, *, finalize=False, chat_id=None, reply_to=None, **kwargs
    ):
        self.streamed.append((chat_id, text, finalize))
        return True


class NativeStructuredAdapter(GateAdapter):
    def __init__(self):
        super().__init__()
        self.native = []

    async def send_card(self, chat_id, card, metadata=None):
        self.native.append(("card", card))
        return SendResult(success=True)

    async def send_poll(self, chat_id, question, options, metadata=None):
        self.native.append(("poll", question, options))
        return SendResult(success=True)

    async def send_location(self, chat_id, latitude, longitude, *, name=None, address=None, metadata=None):
        self.native.append(("location", name, address))
        return SendResult(success=True)

    async def send_effect(self, chat_id, text, effect, metadata=None):
        self.native.append(("effect", text, effect))
        return SendResult(success=True)

    async def send_model_picker(self, chat_id, providers, current_model, current_provider, session_key, callback, metadata=None):
        self.native.append(("model", providers, current_model, current_provider))
        return SendResult(success=True)

    async def send_choice_picker(self, chat_id, title, choices, session_key, callback, metadata=None):
        self.native.append(("choice", title, choices))
        return SendResult(success=True)


class NativeTitleAdapter(GateAdapter):
    def __init__(self):
        super().__init__()
        self.titles = []

    async def create_handoff_thread(self, parent_chat_id, name):
        self.titles.append(("create", parent_chat_id, name))
        return "thread-1"

    async def rename_thread(self, chat_id, thread_id, title):
        self.titles.append(("rename", chat_id, thread_id, title))
        return True

    async def rename_dm_topic(self, chat_id, thread_id, name):
        self.titles.append(("topic", chat_id, thread_id, name))


class RelayFrameTransport:
    def __init__(self):
        self.frames = []
        self._identities = [("slack", "hermes")]

    def descriptor_for_platform(self, platform):
        return None

    async def send_outbound(self, frame, platform=None):
        self.frames.append((frame, platform))
        return {"success": True, "message_id": "relay-1"}

    async def send_follow_up(self, frame, platform=None):
        self.frames.append((frame, platform))
        return {"success": True, "message_id": "follow-up-1"}


def _relay_adapter():
    descriptor = CapabilityDescriptor(
        contract_version=1, platform="slack", label="Slack", max_message_length=4000,
        supports_draft_streaming=True, supports_edit=True, supports_threads=True,
        markdown_dialect="mrkdwn", len_unit="chars",
        supported_ops=("send", "edit", "draft", "prompt", "typing"),
    )
    transport = RelayFrameTransport()
    adapter = RelayAdapter(PlatformConfig(enabled=True, extra={}), descriptor, transport)
    adapter._platform_by_chat["paul"] = "slack"
    return adapter, transport


def test_visible_envelope_excludes_routing_identity_and_credentials():
    visible = extract_user_visible_strings({
        "op": "prompt",
        "chat_id": "C-secret",
        "prompt_id": "p-secret",
        "metadata": {"scope_id": "T-secret", "token": "xoxb-secret"},
        "title": "Visible title",
        "options": [{"id": "route-secret", "label": "Visible action"}],
    })
    assert visible == ["Visible title", "Visible action"]


@pytest.mark.asyncio
async def test_common_adapter_send_boundary_applies_rewrite(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "rewrite", "content": "UNVERIFIED\n\noriginal", "reason": "test"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.send(
        "paul",
        "original",
        metadata={"_hermes_session_id": "s1", "_hermes_turn_id": "t1"},
    )
    assert result.success is True
    assert adapter.sent == [
        ("paul", "UNVERIFIED\n\noriginal", {"_hermes_session_id": "s1", "_hermes_turn_id": "t1"})
    ]


@pytest.mark.asyncio
async def test_common_adapter_send_preserves_positional_metadata_api(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: (
            [{"action": "allow"}] if hook_name == "pre_gateway_send" else []
        ),
    )
    adapter = GateAdapter()
    metadata = {"_hermes_session_id": "s1", "_hermes_turn_id": "t1"}

    result = await adapter.send("paul", "original", "reply-1", metadata)

    assert result.success is True
    assert adapter.sent == [("paul", "original", metadata)]


@pytest.mark.asyncio
async def test_common_adapter_edit_boundary_applies_rewrite(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "rewrite", "content": "UNVERIFIED edited", "reason": "test"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.edit_message("paul", "m1", "fixed", finalize=True)
    assert result.success is True
    assert adapter.edited == [("paul", "m1", "UNVERIFIED edited", True)]


@pytest.mark.asyncio
async def test_final_policy_covers_relay_draft_card_interactive_and_media_egress(monkeypatch):
    captured = []

    def invoke(hook_name, **kwargs):
        captured.append((hook_name, kwargs))
        return []

    def final(**kwargs):
        captured.append(("final_gateway_send_policy", kwargs))
        return {"action": "rewrite", "content": "SAFE"}

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_final_gateway_send_policy", final)
    adapter = AllEgressAdapter()

    await adapter.send_for_platform("SLACK", "paul", "fixed")
    await adapter.send_draft("paul", 7, "fixed")
    await adapter.send_native_task_card_progress(
        "paul", [{"title": "fixed"}], fallback_text="fixed"
    )
    await adapter._post_interactive("paul", {"body": {"text": "fixed"}})
    await adapter._send_media(
        "paul", source="https://dead.example/image.png", caption="fixed"
    )

    policy_calls = [payload for name, payload in captured if name == "final_gateway_send_policy"]
    assert [call["operation"] for call in policy_calls] == [
        "send_for_platform", "send_draft", "send_native_task_card_progress",
        "_post_interactive", "_send_media",
    ]
    assert policy_calls[0]["platform"] == "slack"
    assert policy_calls[0]["chat_id"] == "paul"
    assert all("fixed" in call["content"] or "dead.example" in call["content"] for call in policy_calls)
    assert all("fixed" not in repr(item) and "dead.example" not in repr(item) for item in adapter.egress)


@pytest.mark.asyncio
async def test_relay_transport_boundary_gates_prompt_and_textual_status_but_not_empty_typing(monkeypatch):
    calls = []

    def final(**kwargs):
        calls.append(kwargs)
        return {"action": "rewrite", "content": "SAFE"}

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_final_gateway_send_policy", final)
    adapter, transport = _relay_adapter()

    await adapter._send_prompt(
        "paul", prompt_kind="clarify", text="fixed https://127.0.0.1/admin",
        prompt_id="p1", options=[],
    )
    adapter.set_status_text("paul", "fixed https://127.0.0.1/admin")
    await adapter.send_typing("paul")
    adapter.set_status_text("paul", "")
    await adapter.send_typing("paul")
    await adapter.send_follow_up(
        "session-1", "slack.interaction_token", "fixed https://127.0.0.1/admin",
        metadata={"chat_id": "paul"},
    )

    assert [call["operation"] for call in calls] == ["prompt", "typing", "follow_up"]
    assert [frame[0].get("content") for frame in transport.frames] == [
        "SAFE", "SAFE", None, "SAFE",
    ]


@pytest.mark.asyncio
async def test_relay_terminal_boundary_blocks_every_nested_visible_field_without_gating_routing(monkeypatch):
    calls = []

    def final(**kwargs):
        calls.append(kwargs)
        return {"action": "rewrite", "content": "SAFE"}

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_final_gateway_send_policy", final)
    adapter, transport = _relay_adapter()
    unsafe = "fixed https://127.0.0.1/admin"

    frames = [
        {
            "op": "task_card", "chat_id": "paul", "card_id": unsafe,
            "chunks": [{
                "id": unsafe, "title": unsafe, "body": unsafe,
                "actions": [{"action_id": unsafe, "label": unsafe}],
            }],
            "metadata": {"scope_id": unsafe, "credential": unsafe},
        },
        {
            "op": "send_media", "chat_id": "paul", "media_kind": "image",
            "source_url": unsafe, "content": unsafe, "filename": unsafe,
            "metadata": {"scope_id": unsafe},
        },
        {
            "op": "prompt", "chat_id": "paul", "content": unsafe,
            "prompt_id": unsafe, "prompt_kind": "clarify",
            "options": [{"id": unsafe, "label": unsafe}],
            "metadata": {"scope_id": unsafe},
        },
        {
            "op": "interactive", "chat_id": "paul",
            "interactive": {"title": unsafe, "body": {"text": unsafe}},
            "metadata": {"scope_id": unsafe},
        },
    ]
    for frame in frames:
        await adapter._send_outbound_frame(frame, platform="slack")

    assert len(calls) == 4
    assert all(unsafe in call["content"] for call in calls)
    assert all(sent_frame["content"] == "SAFE" for sent_frame, _ in transport.frames)
    assert all(sent_frame["op"] == "send" for sent_frame, _ in transport.frames)
    assert all(sent_frame["metadata"]["scope_id"] == unsafe for sent_frame, _ in transport.frames)
    assert all("chunks" not in sent_frame and "source_url" not in sent_frame and "options" not in sent_frame
               for sent_frame, _ in transport.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,dynamic", [("fixed configured", ""), ("", "fixed dynamic")])
async def test_native_slack_status_text_crosses_terminal_policy_before_api_io(
    monkeypatch, configured, dynamic
):
    seen = []
    client = SimpleNamespace(assistant_threads_setStatus=AsyncMock())
    adapter = object.__new__(SlackAdapter)
    adapter._app = object()
    adapter.config = SimpleNamespace(typing_status_text=configured)
    adapter._status_text = {"C1": dynamic} if dynamic else {}
    adapter._active_status_threads = {}
    adapter._ACTIVE_STATUS_THREADS_MAX = 1000
    adapter._channel_team = {"C1": "T1"}
    adapter._is_ignored_channel = lambda _chat_id: False
    adapter._resolve_thread_ts = lambda **_kwargs: "171.1"
    adapter._metadata_team_id = lambda _metadata: "T1"
    adapter._workspace_thread_key = lambda team, chat, thread: (team, chat, thread)
    adapter._get_client = lambda *_args, **_kwargs: client

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: seen.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    monkeypatch.setattr(
        "gateway.platforms.base._outbound_gate_required_for_target",
        lambda _platform, _chat_id: True,
    )

    await adapter.send_typing("C1", metadata={"message_id": "171.1"})

    assert seen and seen[-1]["operation"] == "send_typing"
    assert seen[-1]["content"] == (dynamic or configured)
    client.assistant_threads_setStatus.assert_awaited_once_with(
        channel_id="C1", thread_ts="171.1", status="SAFE"
    )


@pytest.mark.asyncio
async def test_native_stream_frame_cannot_bypass_terminal_policy(monkeypatch):
    unsafe = "fixed https://127.0.0.1/admin"
    calls = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    adapter = NativeStreamAdapter()

    result = await adapter.send_stream_frame(unsafe, chat_id="paul", finalize=True)

    assert result is True
    assert calls[-1]["operation"] == "send_stream_frame"
    assert calls[-1]["content"] == unsafe
    assert adapter.streamed == [("paul", "SAFE", True)]
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_standalone_sender_cannot_bypass_terminal_policy(monkeypatch):
    from gateway import run as gateway_run
    from gateway.platform_registry import platform_registry
    from tools.send_message_tool import _send_via_adapter

    unsafe = "fixed https://127.0.0.1/admin"
    delivered = []

    async def standalone_sender(_config, chat_id, content, **_kwargs):
        delivered.append((chat_id, content))
        return {"success": True}

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    monkeypatch.setattr(
        platform_registry,
        "get",
        lambda _name: SimpleNamespace(standalone_sender_fn=standalone_sender),
    )
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "rewrite", "content": "SAFE"},
    )

    result = await _send_via_adapter(
        SimpleNamespace(value="telegram"), SimpleNamespace(), "paul", unsafe
    )

    assert result == {"success": True}
    assert delivered == [("paul", "SAFE")]


@pytest.mark.asyncio
async def test_send_message_direct_weixin_route_crosses_one_terminal_envelope(monkeypatch):
    from tools import send_message_tool as send_tool

    delivered = []
    unsafe = "fixed https://127.0.0.1/admin"

    async def direct(_config, chat_id, content, media_files=None):
        delivered.append((chat_id, content, media_files))
        return {"success": True}

    monkeypatch.setattr(send_tool, "_send_weixin", direct)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "rewrite", "content": "SAFE"},
    )

    result = await send_tool._send_to_platform(
        Platform.WEIXIN, SimpleNamespace(), "paul", unsafe,
        media_files=["https://127.0.0.1/private.png"],
    )

    assert result == {"success": True}
    assert delivered == [("paul", "SAFE", [])]


@pytest.mark.asyncio
async def test_plugin_send_message_handler_cannot_use_nested_raw_args_after_rewrite(monkeypatch):
    from gateway.platform_registry import platform_registry
    from tools import send_message_tool as send_tool

    unsafe = "fixed https://127.0.0.1/admin"
    delivered = []

    async def handler(args, *_rest):
        delivered.append(args)
        return {"success": True}

    monkeypatch.setattr(
        platform_registry, "get",
        lambda _name: SimpleNamespace(max_message_length=0, send_message_handler=handler),
    )
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "rewrite", "content": "SAFE"},
    )
    class CustomPlatform:
        value = "custom"

    platform = CustomPlatform()
    args = {"target": "paul", "message": unsafe, "card": {"title": unsafe}}

    result = await send_tool._send_to_platform(
        platform, SimpleNamespace(), "paul", unsafe, args=args,
    )

    assert result == {"success": True}
    assert delivered == [{"target": "paul", "message": "SAFE", "card": {"title": "SAFE"}}]


@pytest.mark.asyncio
async def test_send_message_terminal_envelope_is_not_double_applied(monkeypatch):
    from gateway import run as gateway_run
    from gateway.platform_registry import platform_registry
    from tools import send_message_tool as send_tool

    calls = []
    delivered = []

    async def standalone(_config, _chat_id, content, **_kwargs):
        delivered.append(content)
        return {"success": True}

    class CustomPlatform:
        value = "custom"

    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: None)
    monkeypatch.setattr(
        platform_registry, "get",
        lambda _name: SimpleNamespace(
            max_message_length=0,
            send_message_handler=None,
            standalone_sender_fn=standalone,
        ),
    )
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "allow"},
    )

    result = await send_tool._send_to_platform(
        CustomPlatform(), SimpleNamespace(), "paul", "ordinary message",
    )

    assert result == {"success": True}
    assert delivered == ["ordinary message"]
    assert [call["operation"] for call in calls] == ["send_message_tool"]


@pytest.mark.asyncio
async def test_native_structured_methods_cannot_bypass_with_empty_top_level_content(monkeypatch):
    unsafe = "fixed https://127.0.0.1/admin"
    calls = []
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    adapter = NativeStructuredAdapter()

    await adapter.send_card("paul", {"cardsV2": [{"header": {"title": unsafe}}]})
    await adapter.send_poll("paul", "", [unsafe])
    await adapter.send_location("paul", 1.0, 2.0, name="", address=unsafe)
    await adapter.send_effect("paul", unsafe, "confetti")
    await adapter.send_model_picker("paul", [{"label": unsafe}], unsafe, "p", "s", None)
    await adapter.send_choice_picker("paul", "", [{"label": unsafe}], "s", None)

    assert [call["operation"] for call in calls] == [
        "send_card", "send_poll", "send_location", "send_effect",
        "send_model_picker", "send_choice_picker",
    ]
    assert adapter.native == [
        ("location", "", "SAFE"),
        ("effect", "SAFE", "SAFE"),
    ]
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)] * 4


@pytest.mark.asyncio
async def test_native_thread_lifecycle_keeps_return_contract_but_sanitizes_titles(monkeypatch):
    unsafe = "fixed https://127.0.0.1/admin"
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "rewrite", "content": "SAFE"},
    )
    adapter = NativeTitleAdapter()

    assert await adapter.create_handoff_thread("paul", unsafe) == "thread-1"
    assert await adapter.rename_thread("paul", "thread-1", unsafe) is True
    assert await adapter.rename_dm_topic("paul", "thread-1", unsafe) is None
    assert adapter.titles == [
        ("create", "paul", "SAFE"),
        ("rename", "paul", "thread-1", "SAFE"),
        ("topic", "paul", "thread-1", "SAFE"),
    ]


@pytest.mark.asyncio
async def test_private_inbound_handler_is_not_misclassified_as_outbound(monkeypatch):
    unsafe = "fixed https://127.0.0.1/admin"
    received = []
    calls = []

    async def _handle_inbound(self, chat_id, content):
        received.append((chat_id, content))
        return "handled"

    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    monkeypatch.setattr(GateAdapter, "_handle_inbound", _handle_inbound, raising=False)

    assert await GateAdapter()._handle_inbound("paul", unsafe) == "handled"
    assert received == [("paul", unsafe)]
    assert calls == []


@pytest.mark.asyncio
async def test_unknown_async_native_publisher_added_after_class_creation_is_gated(monkeypatch):
    unsafe = "fixed https://127.0.0.1/admin"
    delivered = []
    calls = []

    async def publish_native(self, chat_id, content, metadata=None):
        delivered.append((chat_id, content, metadata))
        return True

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    monkeypatch.setattr(GateAdapter, "publish_native", publish_native, raising=False)

    assert await GateAdapter().publish_native("paul", unsafe) is True
    assert delivered == [("paul", "SAFE", None)]
    assert calls[-1]["operation"] == "publish_native"


@pytest.mark.asyncio
async def test_unknown_post_class_publisher_without_named_destination_fails_closed(monkeypatch):
    delivered = []

    async def publish_opaque(self, payload):
        delivered.append(payload)
        return True

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: {"action": "rewrite", "content": "SAFE"},
    )
    monkeypatch.setattr(GateAdapter, "publish_opaque", publish_opaque, raising=False)

    assert await GateAdapter().publish_opaque("fixed https://127.0.0.1/admin") is True
    assert delivered == ["SAFE"]


@pytest.mark.asyncio
async def test_unknown_safe_non_content_method_added_after_class_creation_is_unchanged(monkeypatch):
    calls = []

    async def mark_native_read(self, chat_id, message_id):
        return (chat_id, message_id)

    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **kwargs: calls.append(kwargs) or {"action": "rewrite", "content": "SAFE"},
    )
    monkeypatch.setattr(GateAdapter, "mark_native_read", mark_native_read, raising=False)

    assert await GateAdapter().mark_native_read("paul", "message-1") == ("paul", "message-1")
    assert calls == []


def test_native_direct_io_inventory_requires_terminal_payload_policy_calls():
    root = Path(__file__).resolve().parents[2]
    required = {
        "plugins/platforms/slack/adapter.py": {
            "_set_assistant_suggested_prompts",
            "_handle_slash_confirm_action",
            "_handle_approval_action",
            "_update_clarify_message",
        },
    }
    for relative, method_names in required.items():
        source = (root / relative).read_text()
        tree = ast.parse(source)
        methods = {
            node.name: ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for method_name in method_names:
            assert "apply_terminal_outbound_payload_policy" in methods[method_name], (
                relative, method_name
            )


def test_terminal_transport_inventory_rejects_synthetic_direct_sdk_publish():
    source = """
async def bypass(client, chat_id, content):
    await client.send_message(chat_id=chat_id, text=content)
"""
    assert scan_terminal_transport_inventory(
        source, relative_path="plugins/unsafe_transport.py"
    ) == ["plugins/unsafe_transport.py:3:bypass:send_message"]


def test_terminal_transport_inventory_passes_reviewed_repository_sources():
    from plugins import outbound_message_gate as gate_mod

    root = Path(__file__).resolve().parents[2]
    violations = []
    for relative in gate_mod.GATE_BUILD_SOURCE_PATHS:
        source = (root / relative).read_text()
        violations.extend(
            scan_terminal_transport_inventory(source, relative_path=relative)
        )
    assert violations == []


@pytest.mark.asyncio
async def test_only_required_gate_can_decide_after_ordinary_transformations(monkeypatch):
    seen_by_gate = []

    def invoke(hook_name, **kwargs):
        if hook_name == "pre_gateway_send":
            return [{"action": "rewrite", "content": kwargs["content"] + " fixed"}]
        return []

    def final(**kwargs):
        seen_by_gate.append(kwargs["content"])
        return {"action": "rewrite", "content": "UNVERIFIED"}

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_final_gateway_send_policy", final)
    adapter = GateAdapter()
    await adapter.send("paul", "original")

    assert seen_by_gate == ["original fixed"]
    assert adapter.sent == [("paul", "UNVERIFIED", None)]


@pytest.mark.asyncio
async def test_streaming_edit_boundary_carries_same_turn_metadata(monkeypatch):
    captured = {}

    def invoke(hook_name, **kwargs):
        if hook_name == "pre_gateway_send":
            captured.update(kwargs)
        return [{"action": "allow"}] if hook_name == "pre_gateway_send" else []

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    adapter = GateAdapter()
    metadata = {
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
        "_interim_send": True,
    }
    result = await adapter.edit_message(
        "paul", "m1", "interim", finalize=False, metadata=metadata
    )

    assert result.success is True
    assert captured["operation"] == "edit_message"
    assert captured["metadata"] == metadata


@pytest.mark.asyncio
async def test_common_adapter_boundary_fails_closed_when_hook_invocation_raises(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    adapter = GateAdapter()

    result = await adapter.send("paul", "fixed")

    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


@pytest.mark.asyncio
async def test_common_adapter_boundary_blocks_when_required_gate_is_missing(monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base._outbound_gate_required_for_target",
        lambda _platform, _chat_id: True,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_final_gateway_send_policy",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("missing")),
    )
    adapter = GateAdapter()

    result = await adapter.send("paul", "ordinary message")

    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


@pytest.mark.asyncio
async def test_common_adapter_boundary_blocks_on_plugin_failure(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda hook_name, **kwargs: [
            {"action": "block", "reason": "gate unavailable"}
        ] if hook_name == "pre_gateway_send" else [],
    )
    adapter = GateAdapter()
    result = await adapter.send("paul", "fixed")
    assert result.success is True
    assert adapter.sent == [("paul", _SAFE_OUTBOUND_POLICY_NOTICE, None)]


def test_pre_gateway_send_is_a_fail_closed_plugin_hook():
    assert "pre_gateway_send" in plugins_mod.VALID_HOOKS
    assert "final_gateway_send_policy" in plugins_mod.VALID_HOOKS
    assert "final_gateway_send_policy" in plugins_mod._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS


def test_canonical_policy_config_reads_legacy_alias_and_preserves_recipient_case(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "plugins": {
                "entries": {
                    "outbound_message_gate": {
                        "settings": {"protected_targets": ["slack:C123"]}
                    }
                }
            }
        },
    )
    assert _outbound_gate_required_for_target("SLACK", "C123") is True
    assert _outbound_gate_required_for_target("SLACK", "c123") is False


def test_unreadable_required_policy_config_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: (_ for _ in ()).throw(ValueError("malformed")),
    )
    with pytest.raises(RuntimeError, match="outbound policy config unavailable"):
        _outbound_gate_required_for_target("slack", "C123")


def test_protected_targets_make_bundled_gate_mandatory_without_plugins_enabled():
    config = {
        "plugins": {
            "entries": {
                "outbound-message-gate": {
                    "settings": {"protected_targets": ["telegram:paul"]}
                }
            }
        }
    }
    assert plugins_mod._outbound_gate_required_at_startup(config) is True


def test_plugin_discovery_atomically_loads_and_validates_required_gate(monkeypatch):
    manager = plugins_mod.PluginManager()
    manifest = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="bundled", key="outbound-message-gate"
    )
    monkeypatch.setattr(manager, "_collect_directory_manifests", lambda: [manifest])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(plugins_mod, "_get_disabled_plugins", lambda: set())
    monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "plugins": {
                "entries": {
                    "outbound-message-gate": {
                        "settings": {"protected_targets": ["telegram:paul"]}
                    }
                }
            }
        },
    )
    monkeypatch.setattr(manager, "_warn_python_dependencies", lambda _manifest: None)
    monkeypatch.setattr(manager, "_validate_plugin_config_schema", lambda _manifest: None)
    loaded = []

    def load(required_manifest):
        loaded.append(required_manifest.name)
        manager._register_final_gateway_send_policy(
            required_manifest, lambda **_kwargs: {"action": "allow"}
        )

    monkeypatch.setattr(manager, "_load_plugin", load)
    manager._discover_and_load_inner()

    assert loaded == ["outbound-message-gate"]


def test_required_gate_winner_cannot_be_replaced_by_user_manifest(monkeypatch):
    manager = plugins_mod.PluginManager()
    bundled = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="bundled", key="outbound-message-gate",
        path="/bundled/outbound-message-gate",
    )
    spoof = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="user", key="outbound-message-gate",
        path="/user/outbound-message-gate",
    )
    monkeypatch.setattr(manager, "_collect_directory_manifests", lambda: [bundled, spoof])
    monkeypatch.setattr(manager, "_scan_entry_points", lambda: [])
    monkeypatch.setattr(plugins_mod, "_get_disabled_plugins", lambda: set())
    monkeypatch.setattr(plugins_mod, "_get_enabled_plugins", lambda: None)
    monkeypatch.setattr(plugins_mod, "_outbound_gate_required_at_startup", lambda: True)
    monkeypatch.setattr(manager, "_warn_python_dependencies", lambda _manifest: None)
    monkeypatch.setattr(manager, "_validate_plugin_config_schema", lambda _manifest: None)
    loaded = []

    def load(manifest):
        loaded.append((manifest.source, manifest.path))
        manager._register_final_gateway_send_policy(manifest, lambda **_kwargs: {"action": "allow"})

    monkeypatch.setattr(manager, "_load_plugin", load)
    manager._discover_and_load_inner()

    assert loaded == [("bundled", "/bundled/outbound-message-gate")]


def test_final_policy_registration_and_result_identity_are_loader_owned():
    manager = plugins_mod.PluginManager()
    spoof = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="user", key="outbound-message-gate"
    )
    with pytest.raises(PermissionError, match="bundled outbound-message-gate"):
        manager._register_final_gateway_send_policy(
            spoof, lambda **_kwargs: {"policy_id": "outbound-message-gate", "action": "allow"}
        )

    bundled = plugins_mod.PluginManifest(
        name="outbound-message-gate", source="bundled", key="outbound-message-gate"
    )
    manager._register_final_gateway_send_policy(
        bundled,
        lambda **_kwargs: {"policy_id": "spoofed-return-id", "action": "allow"},
    )
    assert manager.invoke_final_gateway_send_policy(content="hello") == {"action": "allow"}


def test_live_safe_preflight_harness_never_has_a_delivery_transport():
    from scripts.outbound_gate_preflight import run_preflight

    report = run_preflight()
    assert report["transport"] == "in-memory-only"
    assert report["dead_link"]["action"] == "rewrite"
    assert "dead.example" not in report["dead_link"]["content"]
    assert report["naked_fixed_claim"]["content"].startswith("UNVERIFIED")
    assert report["pass"] is True


def test_live_safe_preflight_script_runs_from_its_documented_direct_path():
    repo = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(repo / "scripts" / "outbound_gate_preflight.py")],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["transport"] == "in-memory-only"
    assert report["pass"] is True


def test_receipt_context_is_bound_to_every_turn_metadata_mapping():
    status = {}
    progress = {"thread_id": "topic-1"}
    bind_outbound_receipt_context(
        status,
        progress,
        session_id="session-1",
        turn_id="turn-1",
    )
    assert status == {
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
    }
    assert progress == {
        "thread_id": "topic-1",
        "_hermes_session_id": "session-1",
        "_hermes_turn_id": "turn-1",
    }
