from __future__ import annotations

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    bind_outbound_receipt_context,
)
from hermes_cli import plugins as plugins_mod


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
        lambda hook_name, **kwargs: [{"action": "allow"}],
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
async def test_streaming_edit_boundary_carries_same_turn_metadata(monkeypatch):
    captured = {}

    def invoke(hook_name, **kwargs):
        if hook_name == "pre_gateway_send":
            captured.update(kwargs)
        return [{"action": "allow"}]

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
    assert captured["operation"] == "edit"
    assert captured["metadata"] == metadata


@pytest.mark.asyncio
async def test_common_adapter_boundary_fails_closed_when_hook_invocation_raises(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    adapter = GateAdapter()

    result = await adapter.send("paul", "fixed")

    assert result.success is False
    assert "policy unavailable" in (result.error or "")
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_common_adapter_boundary_blocks_when_required_gate_is_missing(monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base._outbound_gate_required_for_target",
        lambda _platform, _chat_id: True,
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda _name: False)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_args, **_kwargs: [])
    adapter = GateAdapter()

    result = await adapter.send("paul", "ordinary message")

    assert result.success is False
    assert "required" in (result.error or "")
    assert adapter.sent == []


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
    assert result.success is False
    assert "gate unavailable" in (result.error or "")
    assert adapter.sent == []


def test_pre_gateway_send_is_a_fail_closed_plugin_hook():
    assert "pre_gateway_send" in plugins_mod.VALID_HOOKS
    assert "pre_gateway_send" in plugins_mod._HOOK_TIMEOUT_FAIL_CLOSED_HOOKS


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
