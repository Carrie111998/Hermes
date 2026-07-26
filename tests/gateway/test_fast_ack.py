from __future__ import annotations

import asyncio
from types import SimpleNamespace

from gateway.fast_ack import deliver_fast_ack, resolve_fast_ack_config


class _Adapter:
    def __init__(self):
        self.sent = []

    async def send(self, *, chat_id, content, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SimpleNamespace(success=True)


class _FailingAdapter:
    async def send(self, **_kwargs):
        raise RuntimeError("network unavailable")


def test_fast_ack_resolves_platform_override_and_text():
    config = {
        "display": {
            "fast_ack": False,
            "platforms": {
                "telegram": {
                    "fast_ack": True,
                    "fast_ack_delay_seconds": 0.1,
                    "fast_ack_text": "收到，背景處理中。",
                }
            },
        }
    }
    resolved = resolve_fast_ack_config(config, "telegram")
    assert resolved.enabled is True
    assert resolved.delay_seconds == 0.1
    assert resolved.text == "收到，背景處理中。"


def test_fast_ack_sends_once_while_turn_is_silent():
    adapter = _Adapter()
    config = resolve_fast_ack_config(
        {"display": {"fast_ack": True, "fast_ack_delay_seconds": 0.1}},
        "telegram",
    )
    sent = asyncio.run(
        deliver_fast_ack(
            adapter=adapter,
            chat_id="chat-1",
            metadata={"thread_id": "2"},
            config=config,
            run_is_current=lambda: True,
            interim_is_visible=lambda: False,
            run_is_finished=lambda: False,
        )
    )
    assert sent is True
    assert len(adapter.sent) == 1


def test_fast_ack_is_suppressed_after_real_interim_or_completion():
    for interim, finished in ((True, False), (False, True)):
        adapter = _Adapter()
        config = resolve_fast_ack_config(
            {"display": {"fast_ack": True, "fast_ack_delay_seconds": 0.1}},
            "telegram",
        )
        sent = asyncio.run(
            deliver_fast_ack(
                adapter=adapter,
                chat_id="chat-1",
                metadata=None,
                config=config,
                run_is_current=lambda: True,
                interim_is_visible=lambda: interim,
                run_is_finished=lambda: finished,
            )
        )
        assert sent is False
        assert adapter.sent == []


def test_fast_ack_delivery_failure_does_not_fail_the_turn():
    config = resolve_fast_ack_config(
        {"display": {"fast_ack": True, "fast_ack_delay_seconds": 0.1}},
        "telegram",
    )
    sent = asyncio.run(
        deliver_fast_ack(
            adapter=_FailingAdapter(),
            chat_id="chat-1",
            metadata=None,
            config=config,
            run_is_current=lambda: True,
            interim_is_visible=lambda: False,
            run_is_finished=lambda: False,
        )
    )
    assert sent is False
