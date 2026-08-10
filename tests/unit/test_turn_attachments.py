import asyncio

import pytest

from agent.turn_attachments import (
    attach_final_reply_link_buttons, begin_turn, end_turn, snapshot,
)
from gateway.platforms.base import _merge_final_reply_metadata


def _buttons(url="https://pages.example/p/abc"):
    return [{"text": "Open", "kind": "web_app", "url": url, "row": 0}]


def test_attachment_is_host_bound_and_one_shot():
    assert attach_final_reply_link_buttons(_buttons()) is False
    token = begin_turn()
    try:
        assert attach_final_reply_link_buttons(_buttons()) is True
        assert snapshot()["link_buttons"] == _buttons()
    finally:
        end_turn(token)
    assert snapshot() == {}


def test_concurrent_turns_do_not_mix():
    async def turn(url):
        token = begin_turn()
        try:
            await asyncio.sleep(0)
            attach_final_reply_link_buttons(_buttons(url))
            await asyncio.sleep(0)
            return snapshot()
        finally:
            end_turn(token)
    async def run_both():
        return await asyncio.gather(turn("https://one.example/p/a"), turn("https://two.example/p/b"))
    one, two = asyncio.run(run_both())
    assert one["link_buttons"][0]["url"].startswith("https://one.example")
    assert two["link_buttons"][0]["url"].startswith("https://two.example")


def test_rejects_unsafe_link_button():
    token = begin_turn()
    try:
        with pytest.raises(ValueError):
            attach_final_reply_link_buttons(_buttons("http://pages.example/p/a"))
    finally:
        end_turn(token)


def test_final_metadata_merge_uses_only_reserved_event_value():
    event = type("Event", (), {"metadata": {"link_buttons": _buttons(), "_hermes_final_reply_metadata": {"link_buttons": _buttons("https://trusted.example/p/a")}}})()
    merged = _merge_final_reply_metadata({"thread_id": "1"}, event)
    assert merged["link_buttons"][0]["url"].startswith("https://trusted.example")
    assert merged["thread_id"] == "1"


def test_streaming_only_attaches_to_final_send():
    from gateway.stream_consumer import GatewayStreamConsumer
    token = begin_turn()
    try:
        consumer = GatewayStreamConsumer(object(), "chat", metadata={"thread_id": "1"})
        attach_final_reply_link_buttons(_buttons())
        assert "link_buttons" not in consumer._metadata_for_send(final=False)
        assert consumer._metadata_for_send(final=True)["link_buttons"] == _buttons()
    finally:
        end_turn(token)


def test_streaming_final_edit_carries_attachment():
    from gateway.stream_consumer import GatewayStreamConsumer
    class Adapter:
        async def edit_message(self, **kwargs):
            self.kwargs = kwargs
            return type("Result", (), {"success": True, "continuation_message_ids": (), "message_id": "1"})()
    async def scenario():
        adapter = Adapter()
        consumer = GatewayStreamConsumer(adapter, "chat")
        consumer._message_id = "1"
        consumer._last_sent_text = "draft"
        await consumer._send_or_edit("done", finalize=True, is_turn_final=True)
        return adapter.kwargs
    token = begin_turn()
    try:
        attach_final_reply_link_buttons(_buttons())
        kwargs = asyncio.run(scenario())
        assert kwargs["metadata"]["link_buttons"] == _buttons()
    finally:
        end_turn(token)
