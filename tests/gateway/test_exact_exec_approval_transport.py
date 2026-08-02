import hashlib
from pathlib import Path

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


class _TestAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.document = None

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="text")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _NativeDocumentAdapter(_TestAdapter):
    async def send_document(
        self,
        chat_id,
        file_path,
        caption=None,
        file_name=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ):
        self.document = {
            "bytes": Path(file_path).read_bytes(),
            "caption": caption,
            "file_name": file_name,
            "metadata": metadata,
        }
        return SendResult(success=True, message_id="document")


class _FallbackDocumentAdapter(_TestAdapter):
    async def send_document(
        self,
        chat_id,
        file_path,
        caption=None,
        file_name=None,
        reply_to=None,
        metadata=None,
        **kwargs,
    ):
        return await super().send_document(
            chat_id=chat_id,
            file_path=file_path,
            caption=caption,
            file_name=file_name,
            reply_to=reply_to,
            metadata=metadata,
        )


@pytest.mark.asyncio
async def test_shared_exact_transport_delivers_canonical_bytes_through_native_document():
    adapter = _NativeDocumentAdapter()
    approval_id = "a" * 32
    command = "  first\nsecond  \x00"
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()

    result = await adapter.send_exact_exec_approval(
        "chat",
        command,
        approval_id=approval_id,
        command_sha256=digest,
        metadata={"thread_id": "thread"},
    )

    assert result.success is True
    assert adapter.document["bytes"] == command.encode("utf-8")
    assert adapter.document["file_name"] == f"hermes-exact-operation-{approval_id}.txt"
    assert digest in adapter.document["caption"]
    assert command not in adapter.document["caption"]
    assert adapter.document["metadata"] == {
        "thread_id": "thread",
        "_hermes_exact_exec_approval": True,
    }


@pytest.mark.asyncio
async def test_shared_exact_transport_fails_closed_without_native_document_delivery():
    adapter = _TestAdapter()
    command = "opaque bytes"

    result = await adapter.send_exact_exec_approval(
        "chat",
        command,
        approval_id="b" * 32,
        command_sha256=hashlib.sha256(command.encode("utf-8")).hexdigest(),
    )

    assert result.success is False
    assert "no byte-preserving" in result.error


@pytest.mark.asyncio
async def test_shared_exact_transport_rejects_text_fallback_from_document_override():
    adapter = _FallbackDocumentAdapter()
    command = "opaque bytes"

    result = await adapter.send_exact_exec_approval(
        "chat",
        command,
        approval_id="c" * 32,
        command_sha256=hashlib.sha256(command.encode("utf-8")).hexdigest(),
    )

    assert result.success is False
    assert "no byte-preserving" in result.error


def test_every_interactive_gateway_approval_adapter_has_an_exact_attachment_path():
    from gateway.platforms.qqbot.adapter import QQAdapter
    from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter
    from gateway.relay.adapter import RelayAdapter
    from plugins.platforms.discord.adapter import DiscordAdapter
    from plugins.platforms.feishu.adapter import FeishuAdapter
    from plugins.platforms.matrix.adapter import MatrixAdapter
    from plugins.platforms.slack.adapter import SlackAdapter
    from plugins.platforms.teams.adapter import TeamsAdapter
    from plugins.platforms.telegram.adapter import TelegramAdapter

    native_document_adapters = (
        QQAdapter,
        WhatsAppCloudAdapter,
        FeishuAdapter,
        MatrixAdapter,
        SlackAdapter,
        TeamsAdapter,
        TelegramAdapter,
    )
    for adapter_type in native_document_adapters:
        assert adapter_type.send_document is not BasePlatformAdapter.send_document

    assert DiscordAdapter.send_exact_exec_approval is not BasePlatformAdapter.send_exact_exec_approval
    assert (
        RelayAdapter._send_exact_exec_approval_document
        is not BasePlatformAdapter._send_exact_exec_approval_document
    )
