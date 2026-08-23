"""Cron results must arrive as notifications, not silently.

Field report: with the default ``notifications_mode: important`` the Telegram
adapter sends every message with ``disable_notification=True`` unless the
caller opts in via ``metadata["notify"]`` (see
``TelegramAdapter._notification_kwargs``). The gateway sets that flag on its
FINAL reply — the adapter's own comments at the streaming call sites read
"the FINAL reply (metadata['notify'])".

``_deliver_result`` never set it. A cron job's delivered result IS a final,
user-facing delivery, so scheduled reports (order alerts, monitoring digests,
morning briefs) landed on the phone with no notification at all — the exact
messages the "important" mode exists to surface. The user only saw them by
opening the chat manually.

Both routing lanes are pinned: the ambiguous-topic Direct-Messages lane and
the ordinary thread / non-thread lane.
"""

import asyncio
from concurrent.futures import Future
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cron.scheduler import _deliver_result
from gateway.config import Platform


def _telegram_adapter():
    adapter = AsyncMock()
    adapter.fronts_platform = lambda p: False
    return adapter


def _gateway_config():
    """Telegram natively configured — the live-adapter lane is gated on it."""
    from gateway.config import PlatformConfig

    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda p: None
    return config


def _run_delivery(job, *, dm_topic: bool):
    """Drive ``_deliver_result`` through the live-adapter lane and capture the
    routing metadata handed to the DeliveryRouter."""
    captured = {}
    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as e:  # noqa: BLE001
            future.set_exception(e)
        return future

    router = MagicMock()

    async def _deliver_to_platform(target, content, metadata):
        captured["metadata"] = metadata
        return {"success": True, "raw_response": None}

    router._deliver_to_platform = _deliver_to_platform

    with patch("gateway.config.load_gateway_config", return_value=_gateway_config()), \
         patch("cron.scheduler.load_config",
               return_value={"cron": {"wrap_response": False}}), \
         patch("cron.scheduler._is_channel_dm_topic", return_value=dm_topic), \
         patch("gateway.delivery.DeliveryRouter", return_value=router), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        _deliver_result(job, "Nightly report.",
                        adapters={Platform.TELEGRAM: _telegram_adapter()},
                        loop=loop)
    return captured


def _job(chat_id="8654275023", thread_id=None):
    deliver = f"telegram:{chat_id}"
    if thread_id is not None:
        deliver = f"{deliver}:{thread_id}"
    return {
        "id": "notify-job",
        "name": "Monitoring",
        "deliver": deliver,
        "origin": {"platform": "telegram", "chat_id": chat_id},
    }


@pytest.mark.parametrize("thread_id", [None, "42"])
def test_plain_and_threaded_delivery_requests_a_notification(thread_id):
    """The ordinary lane (no topic, or a forum-style thread) must opt in."""
    captured = _run_delivery(_job(thread_id=thread_id), dm_topic=False)
    metadata = captured.get("metadata") or {}
    assert metadata.get("notify") is True, (
        "cron result delivered without metadata['notify'] — under the default "
        "notifications_mode=important the Telegram adapter stamps "
        "disable_notification=True and the report arrives silently"
    )


def test_channel_dm_topic_delivery_requests_a_notification():
    """The Bot API Direct-Messages topic lane (#22773) must opt in too."""
    captured = _run_delivery(_job(thread_id="99"), dm_topic=True)
    metadata = captured.get("metadata") or {}
    assert metadata.get("notify") is True, (
        "DM-topic cron result delivered silently — the topic lane builds its "
        "own metadata dict and must carry notify like the ordinary lane"
    )
