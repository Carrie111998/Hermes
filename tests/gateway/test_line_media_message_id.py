"""Tests for LINE triggering-message-id injection in _prepare_inbound_message_text.

A LINE media message's binary is only reachable through the LINE content API,
keyed by the id of the message that carried it. Without the id on the turn, a
skill that keeps inbound files has nothing to name the file with. Discord
already gets its triggering id this way; LINE did not, so any LINE file-keeping
skill was uncallable however well it was written.

The prefix is deliberately conditional on the message actually carrying media:
text-only turns must keep a byte-stable prefix so prompt caching still hits.
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource

LINE = Platform("line")


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _source(platform: Platform = LINE) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="U0123456789abcdef",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


def _document_event(source: SessionSource, **kwargs) -> MessageEvent:
    return MessageEvent(
        text="",
        source=source,
        message_type=MessageType.DOCUMENT,
        media_urls=["/tmp/hermes-cache/doc_abc_report.pdf"],
        media_types=["application/pdf"],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_id_injected_for_line_media_message():
    runner = _make_runner()
    source = _source()
    event = _document_event(source, message_id="629694223050605090")

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )

    assert result is not None
    assert "[Triggering message id: `629694223050605090`" in result
    assert "LINE content API" in result


@pytest.mark.asyncio
async def test_id_not_injected_for_line_text_message():
    """Text turns must keep a byte-stable prefix so prompt caching still hits."""
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="เย็นนี้ประชุมกี่โมง",
        source=source,
        message_id="629694223050605091",
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )

    assert result is not None
    assert "Triggering message id" not in result


@pytest.mark.asyncio
async def test_id_not_injected_for_non_line_media_message():
    runner = _make_runner()
    source = _source(Platform.TELEGRAM)
    event = _document_event(source, message_id="4242")

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[]
    )

    assert result is not None
    assert "Triggering message id" not in result
