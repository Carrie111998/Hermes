"""Cron delivery contracts around Discord handoff-thread creation."""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cron.scheduler import _deliver_result
from gateway.config import Platform


PARENT_ID = "123456789"
CHILD_ID = "987654321"
RESPONSE = "Here is today's writing exercise."


def _run_delivery(opened_thread_id):
    captured = {}

    class SpyRouter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def _deliver_to_platform(self, target, text, metadata):
            captured["target"] = target
            captured["text"] = text
            captured["metadata"] = metadata
            return SimpleNamespace(
                success=True,
                message_id="delivered-message",
                raw_response=None,
            )

    def run_now(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - surfaced by result()
            future.set_exception(exc)
        return future

    platform_config = MagicMock()
    platform_config.enabled = True
    platform_config.extra = {}
    gateway_config = MagicMock()
    gateway_config.platforms = {Platform.DISCORD: platform_config}

    adapter = MagicMock()
    adapter.name = "Discord"
    loop = MagicMock()
    loop.is_running.return_value = True
    job = {
        "id": "daily-writing",
        "name": "Daily writing prompt",
        "deliver": "origin",
        "origin": {
            "platform": "discord",
            "chat_id": PARENT_ID,
            "chat_type": "group",
            "user_id": "writer",
        },
        "attach_to_session": True,
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=gateway_config),
        patch(
            "cron.scheduler.load_config",
            return_value={"cron": {"wrap_response": False}},
        ),
        patch(
            "cron.scheduler._open_continuable_cron_thread",
            return_value=opened_thread_id,
        ),
        patch("gateway.delivery.DeliveryRouter", SpyRouter),
        patch("agent.async_utils.safe_schedule_threadsafe", side_effect=run_now),
        patch("cron.scheduler._seed_cron_thread_session") as seed_session,
        patch("gateway.mirror.mirror_to_session", return_value=True),
    ):
        _deliver_result(
            job,
            RESPONSE,
            adapters={Platform.DISCORD: adapter},
            loop=loop,
        )

    return captured, seed_session


def test_cron_thread_creation_failure_delivers_full_response_flat_to_parent():
    captured, seed_session = _run_delivery(None)

    assert captured["target"].chat_id == PARENT_ID
    assert captured["target"].thread_id is None
    assert captured["text"] == RESPONSE
    assert "thread_id" not in captured["metadata"]
    seed_session.assert_not_called()


def test_cron_success_delivers_response_and_seeds_session_in_child_thread():
    captured, seed_session = _run_delivery(CHILD_ID)

    assert captured["target"].chat_id == PARENT_ID
    assert captured["target"].thread_id == CHILD_ID
    assert captured["metadata"]["thread_id"] == CHILD_ID
    assert captured["text"] == RESPONSE
    seed_session.assert_called_once()
    assert seed_session.call_args.args[3] == PARENT_ID
    assert seed_session.call_args.args[4] == CHILD_ID
    assert seed_session.call_args.args[5] == RESPONSE
