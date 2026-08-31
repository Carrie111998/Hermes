"""Regression: offering suggested actions must not delay turn completion.

The generation call goes through the auxiliary router and can legitimately
take up to its configured timeout — tens of seconds on a slow or `:free`
backend. Before this fix, `_maybe_offer_suggested_actions` was awaited
inline at the tail of `_process_message_background`, so the turn's
session-busy release, `on_processing_complete` hook, and queued-message
drain all waited on it too. It is now dispatched via `asyncio.create_task`
and never awaited by the turn itself.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from gateway import suggested_actions as sa
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.session import SessionSource, build_session_key


class _ProbeAdapter(BasePlatformAdapter):
    """Minimal concrete adapter that records deliveries (mirrors the
    BaseException-notify regression test's harness)."""

    def __init__(self) -> None:
        super().__init__(PlatformConfig(enabled=True, token="x"), Platform.SLACK)
        self.sent: list[str] = []

    async def start(self):  # pragma: no cover - unused
        pass

    async def stop(self):  # pragma: no cover - unused
        pass

    async def connect(self):  # pragma: no cover - unused
        pass

    async def disconnect(self):  # pragma: no cover - unused
        pass

    async def get_chat_info(self, chat_id):  # pragma: no cover - unused
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)

        class _R:
            success = True
            message_id = "m1"
            raw_response = None

        return _R()

    async def send_typing(self, chat_id, metadata=None):  # pragma: no cover - unused
        pass

    async def send_suggested_actions(self, chat_id, actions, set_id, session_key,
                                     metadata=None, anchor_message_id=None):
        class _R:
            success = True
            message_id = "sa1"

        return _R()


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK, user_id="U1", chat_id="C1",
        user_name="tester", chat_type="channel",
    )


def _make_adapter() -> _ProbeAdapter:
    adapter = _ProbeAdapter()

    async def handler(event):
        return "Done."

    adapter.set_message_handler(handler)
    return adapter


def _event() -> MessageEvent:
    return MessageEvent(text="hello", message_type=MessageType.TEXT, source=_source())


@pytest.mark.asyncio
async def test_turn_completes_without_waiting_for_generation(monkeypatch):
    # Plain threading.Event: `generate` runs off-loop via asyncio.to_thread,
    # so it needs a primitive it can block on synchronously.
    release = threading.Event()

    def slow_generate(reply_text, user_text):
        release.wait(timeout=5.0)
        return ["Retry"]

    monkeypatch.setattr(sa, "is_enabled", lambda: True)
    monkeypatch.setattr(sa, "generate", slow_generate)

    adapter = _make_adapter()
    event = _event()

    started = time.monotonic()
    await asyncio.wait_for(
        adapter._process_message_background(event, build_session_key(event.source)),
        timeout=3.0,
    )
    elapsed = time.monotonic() - started

    assert adapter.sent == ["Done."], (
        "the reply must be delivered without waiting on suggestion generation"
    )
    assert elapsed < 2.0, (
        f"turn took {elapsed:.2f}s — it appears to be waiting on the "
        "5s-blocked suggestion generation instead of returning immediately"
    )

    # Let the still-running background generation finish so it doesn't leak
    # across tests.
    release.set()
    await asyncio.sleep(0.05)
