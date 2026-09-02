"""Live cron delivery through a primary adapter for exact profile routes."""

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from cron.scheduler import ProfileRouteDeliveryContext, _deliver_result
from gateway.config import Platform, PlatformConfig
from gateway.profile_routing import ProfileRoute


CHAT_ID = "1543065293755256852"


class _SendResult:
    def __init__(self, *, success=True, message_id=None, error=None):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = None


def _config(*, enabled=True, routes=None):
    config = MagicMock()
    config.platforms = (
        {Platform.DISCORD: PlatformConfig(enabled=True)} if enabled else {}
    )
    config.profile_routes = list(routes or [])
    config.get_home_channel.return_value = None
    return config


def _job(chat_id=CHAT_ID):
    return {
        "id": "shared-route-job",
        "name": "Shared route",
        "deliver": f"discord:{chat_id}",
    }


def _context(*, profile="fitness", chat_id=CHAT_ID, adapter=True, enabled=True):
    routes = [
        ProfileRoute(
            name="fitness-channel",
            platform="discord",
            chat_id=chat_id,
            profile=profile,
            enabled=enabled,
        )
    ]
    primary_adapter = MagicMock(name="primary-discord-adapter")
    adapters = {Platform.DISCORD: primary_adapter} if adapter else {}
    return (
        ProfileRouteDeliveryContext(
            profile="fitness",
            config=_config(enabled=True, routes=routes),
            adapters=adapters,
        ),
        primary_adapter,
    )


def _run(*, context, own_adapter=None, result=None, chat_id=CHAT_ID):
    loop = MagicMock()
    loop.is_running.return_value = True
    router_calls = []
    router_construction = []
    standalone_calls = []

    def fake_run_coro(coro, _loop):
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    class _Router:
        def __init__(self, config, adapters):
            router_construction.append((config, adapters))

        async def _deliver_to_platform(self, target, text, metadata):
            router_calls.append((target, text, metadata))
            return result or _SendResult(message_id="discord-message-1")

    async def _standalone(*args, **kwargs):
        standalone_calls.append((args, kwargs))
        return {}

    satellite_config = _config(enabled=own_adapter is not None)
    own_adapters = {Platform.DISCORD: own_adapter} if own_adapter is not None else {}
    with patch("gateway.config.load_gateway_config", return_value=satellite_config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("gateway.delivery.DeliveryRouter", _Router), \
         patch("tools.send_message_tool._send_to_platform", _standalone), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(
            _job(chat_id),
            "Scheduled report",
            adapters=own_adapters,
            loop=loop,
            profile_route_context=context,
        )
    return error, router_calls, router_construction, standalone_calls


def test_exact_route_uses_primary_live_adapter_with_positive_evidence():
    context, primary_adapter = _context()

    error, calls, constructed, standalone = _run(context=context)

    assert error is None
    assert len(calls) == 1
    assert constructed == [(context.config, context.adapters)]
    assert constructed[0][1][Platform.DISCORD] is primary_adapter
    assert standalone == []


def test_secondary_own_adapter_wins_over_primary_route():
    context, _primary_adapter = _context()
    own_adapter = MagicMock(name="own-discord-adapter")

    error, calls, constructed, standalone = _run(
        context=context, own_adapter=own_adapter
    )

    assert error is None
    assert len(calls) == 1
    assert constructed[0][1][Platform.DISCORD] is own_adapter
    assert standalone == []


def test_unmatched_target_does_not_use_primary_adapter():
    context, _primary_adapter = _context(chat_id="different-channel")

    error, calls, constructed, standalone = _run(context=context)

    assert error is not None
    assert calls == []
    assert constructed == []
    assert standalone == []


def test_route_for_another_profile_does_not_use_primary_adapter():
    context, _primary_adapter = _context(profile="other")

    error, calls, constructed, standalone = _run(context=context)

    assert error is not None
    assert calls == []
    assert constructed == []
    assert standalone == []


def test_disabled_route_does_not_use_primary_adapter():
    context, _primary_adapter = _context(enabled=False)

    error, calls, constructed, standalone = _run(context=context)

    assert error is not None
    assert calls == []
    assert constructed == []
    assert standalone == []


def test_exact_route_without_primary_adapter_fails_without_standalone():
    context, _primary_adapter = _context(adapter=False)

    error, calls, constructed, standalone = _run(context=context)

    assert error is not None
    assert "primary live adapter" in error
    assert calls == []
    assert constructed == []
    assert standalone == []


def test_primary_route_send_failure_does_not_fall_back_to_satellite_standalone():
    context, _primary_adapter = _context()

    error, calls, _constructed, standalone = _run(
        context=context,
        result=_SendResult(success=False, error="Discord unavailable"),
    )

    assert len(calls) == 1
    assert error is not None
    assert "Discord unavailable" in error
    assert standalone == []
