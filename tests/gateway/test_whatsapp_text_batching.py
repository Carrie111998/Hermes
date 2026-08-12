"""Text-debounce batching for the WhatsApp adapter (issue #35301).

WhatsApp delivers rapid multi-message bursts (forwarded batches, paste-splits)
individually.  Without debounce each fragment triggers a separate agent
invocation, wasting tokens and flooding the user with reply fragments.  This
mirrors the Telegram/WeCom/Feishu pattern.

Batch delays are read from ``config.extra`` (config.yaml), not env vars.
"""

import asyncio

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter
from gateway.session import SessionSource


def _make_adapter(**extra):
    base = {"session_name": "test"}
    base.update(extra)
    return WhatsAppAdapter(PlatformConfig(enabled=True, extra=base))


async def _drain_text_batches(adapter) -> None:
    """Await the adapter's pending flush tasks instead of racing a sleep.

    ``_enqueue_text_event`` dispatches from a background task that first sleeps
    ``_text_batch_delay_seconds``.  These tests used to wait for that with
    ``await asyncio.sleep(0.2)`` against a 0.05s delay; the margin looks
    generous but the task still needs loop turns after its deadline to finish
    dispatching, so on a loaded box ``dispatched`` is still empty when the
    assertion runs.  The adapter already tracks the task in
    ``_pending_text_batch_tasks``, so wait on the work itself.

    Snapshot the values first: the flush pops its own key in a ``finally``.
    ``return_exceptions=True`` keeps this a wait, not an assertion -- a
    superseded chunk's task is cancelled by design.

    Deliberately no fallback when the map is empty -- if nothing was
    scheduled, the caller's assertion must still fail.
    """
    tasks = tuple(adapter._pending_text_batch_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _event(text):
    src = SessionSource(
        platform=Platform.WHATSAPP,
        chat_id="chat123",
        chat_type="dm",
        user_id="user1",
        user_name="tester",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=src)


def test_batch_delays_default_from_config():
    adapter = _make_adapter()
    assert adapter._text_batch_delay_seconds == 5.0
    assert adapter._text_batch_split_delay_seconds == 10.0


def test_batch_delays_overridden_via_config_extra():
    adapter = _make_adapter(
        text_batch_delay_seconds="2.5",
        text_batch_split_delay_seconds=7,
    )
    assert adapter._text_batch_delay_seconds == 2.5
    assert adapter._text_batch_split_delay_seconds == 7.0


def test_invalid_config_value_falls_back_to_default():
    adapter = _make_adapter(
        text_batch_delay_seconds="garbage",
        text_batch_split_delay_seconds=-3,
    )
    assert adapter._text_batch_delay_seconds == 5.0
    assert adapter._text_batch_split_delay_seconds == 10.0


def test_env_var_is_ignored(monkeypatch):
    # Config-only path: the legacy HERMES_* env var must NOT influence delays.
    monkeypatch.setenv("HERMES_WHATSAPP_TEXT_BATCH_DELAY_SECONDS", "99")
    adapter = _make_adapter()
    assert adapter._text_batch_delay_seconds == 5.0


def test_rapid_texts_collapse_into_single_dispatch():
    adapter = _make_adapter(
        text_batch_delay_seconds=0.05,
        text_batch_split_delay_seconds=0.05,
    )
    dispatched = []

    async def _capture(event):
        dispatched.append(event.text)

    adapter.handle_message = _capture

    async def _drive():
        adapter._enqueue_text_event(_event("one"))
        adapter._enqueue_text_event(_event("two"))
        adapter._enqueue_text_event(_event("three"))
        assert dispatched == []  # nothing flushed during the burst
        await _drain_text_batches(adapter)

    asyncio.run(_drive())
    assert dispatched == ["one\ntwo\nthree"]


def test_lone_message_dispatched_alone():
    adapter = _make_adapter(
        text_batch_delay_seconds=0.05,
        text_batch_split_delay_seconds=0.05,
    )
    dispatched = []

    async def _capture(event):
        dispatched.append(event.text)

    adapter.handle_message = _capture

    async def _drive():
        adapter._enqueue_text_event(_event("solo"))
        await _drain_text_batches(adapter)

    asyncio.run(_drive())
    assert dispatched == ["solo"]
