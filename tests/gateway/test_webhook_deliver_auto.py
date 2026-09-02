"""Tests for the webhook adapter's ``deliver: auto`` delivery target.

``deliver: auto`` resolves the delivery platform at send time: the first
platform — in **config declaration order** — that is currently connected
and has a home channel configured.  It exists so a single route config
works for any chat platform the user has set up, without hardcoding
``deliver: telegram`` / ``deliver: discord`` / etc.

Covers:
- Agent-mode ``send()`` resolution to the first declared platform
- The ordering contract: config declaration order wins even when the
  runner's adapter dict is ordered differently (reconnects reorder it)
- Skipping platforms that are not connected or have no home channel
- Skipping the webhook adapter itself (no self-delivery loops)
- Fallback to ``log`` when nothing qualifies (agent mode + direct mode)
- Delivery to a connected platform that is NOT in the builtin deliver
  allow-list (auto bypasses the name gate by construction)
- Direct-delivery (``deliver_only``) end-to-end via HTTP POST
- Startup validation accepts ``deliver_only`` + ``deliver: auto``
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    _BUILTIN_DELIVER_PLATFORMS,
    _INSECURE_NO_AUTH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(routes) -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True,
        extra={"host": "127.0.0.1", "port": 0, "routes": routes},
    )
    return WebhookAdapter(config)


def _auto_route(**overrides):
    route = {
        "secret": _INSECURE_NO_AUTH,
        "deliver": "auto",
        "prompt": "{message}",
    }
    route.update(overrides)
    return route


def _wire_runner(adapter, declared, connected, homes):
    """Attach a gateway_runner shaped like the real one.

    declared:  list[Platform] — config declaration order
    connected: list[Platform] — which of them have live adapters, in
               ADAPTER-DICT insertion order (deliberately independent of
               ``declared`` so ordering tests can diverge the two)
    homes:     dict[Platform, chat_id] — platforms with a home channel
    """
    adapters = {}
    for platform in connected:
        target = AsyncMock()
        target.send = AsyncMock(return_value=SendResult(success=True))
        adapters[platform] = target

    config = SimpleNamespace(
        platforms={p: PlatformConfig(enabled=True) for p in declared},
        get_home_channel=lambda p: (
            HomeChannel(platform=p, chat_id=homes[p], name="Home")
            if p in homes
            else None
        ),
    )
    adapter.gateway_runner = SimpleNamespace(
        adapters=adapters,
        config=config,
        _profile_adapters={},
    )
    return adapters


def _seed_agent_delivery(adapter, chat_id="webhook:r:d-1"):
    adapter._delivery_info[chat_id] = {"deliver": "auto", "deliver_extra": {}}
    return chat_id


# ===================================================================
# Agent-mode send() resolution
# ===================================================================

class TestAutoResolutionAgentMode:

    @pytest.mark.asyncio
    async def test_resolves_first_declared_platform(self):
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM, Platform.DISCORD],
            connected=[Platform.TELEGRAM, Platform.DISCORD],
            homes={Platform.TELEGRAM: "tg-home", Platform.DISCORD: "dc-home"},
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.TELEGRAM].send.assert_awaited_once()
        assert adapters[Platform.TELEGRAM].send.await_args.args[0] == "tg-home"
        adapters[Platform.DISCORD].send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_declaration_order_wins_over_adapter_dict_order(self):
        """The regression the sweeper review asked for.

        A fatal adapter failure pops the adapter from the runner's dict and
        a reconnect appends it at the END — so adapter-dict order is
        failure-history-dependent.  ``auto`` must follow config declaration
        order instead, or one Telegram hiccup silently re-routes every
        delivery to Discord for the life of the process.
        """
        adapter = _make_adapter({})
        # Adapter dict has discord FIRST (telegram failed + reconnected);
        # config declares telegram first.
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM, Platform.DISCORD],
            connected=[Platform.DISCORD, Platform.TELEGRAM],
            homes={Platform.TELEGRAM: "tg-home", Platform.DISCORD: "dc-home"},
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.TELEGRAM].send.assert_awaited_once()
        adapters[Platform.DISCORD].send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_platform_without_connected_adapter(self):
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM, Platform.DISCORD],
            connected=[Platform.DISCORD],  # telegram configured but down
            homes={Platform.TELEGRAM: "tg-home", Platform.DISCORD: "dc-home"},
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.DISCORD].send.assert_awaited_once()
        assert adapters[Platform.DISCORD].send.await_args.args[0] == "dc-home"

    @pytest.mark.asyncio
    async def test_skips_platform_without_home_channel(self):
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM, Platform.DISCORD],
            connected=[Platform.TELEGRAM, Platform.DISCORD],
            homes={Platform.DISCORD: "dc-home"},  # telegram has no home
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.TELEGRAM].send.assert_not_awaited()
        adapters[Platform.DISCORD].send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_webhook_platform_itself(self):
        """Even a webhook entry with a (nonsensical) home channel is skipped."""
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.WEBHOOK, Platform.TELEGRAM],
            connected=[Platform.WEBHOOK, Platform.TELEGRAM],
            homes={Platform.WEBHOOK: "loop", Platform.TELEGRAM: "tg-home"},
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.WEBHOOK].send.assert_not_awaited()
        adapters[Platform.TELEGRAM].send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_log_when_nothing_qualifies(self):
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM],
            connected=[Platform.TELEGRAM],
            homes={},  # connected, but no home channel anywhere
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.TELEGRAM].send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_gateway_runner_falls_back_to_log(self):
        adapter = _make_adapter({})
        adapter.gateway_runner = None
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True

    @pytest.mark.asyncio
    async def test_delivers_to_platform_outside_builtin_allow_list(self):
        """Auto-resolved targets must bypass the known-platform name gate.

        ``send()`` gates explicit deliver names on _BUILTIN_DELIVER_PLATFORMS
        (plus plugin registrations).  A platform like whatsapp_cloud supports
        home channels but is absent from that set — the resolver already
        proved the target is connected with a home, so the resolved name must
        be dispatched directly, not re-validated against the gate.
        """
        assert Platform.WHATSAPP_CLOUD.value not in _BUILTIN_DELIVER_PLATFORMS
        adapter = _make_adapter({})
        adapters = _wire_runner(
            adapter,
            declared=[Platform.WHATSAPP_CLOUD],
            connected=[Platform.WHATSAPP_CLOUD],
            homes={Platform.WHATSAPP_CLOUD: "wa-home"},
        )
        chat_id = _seed_agent_delivery(adapter)

        result = await adapter.send(chat_id, "hello")

        assert result.success is True
        adapters[Platform.WHATSAPP_CLOUD].send.assert_awaited_once()
        assert (
            adapters[Platform.WHATSAPP_CLOUD].send.await_args.args[0]
            == "wa-home"
        )


# ===================================================================
# Direct delivery (deliver_only) end-to-end
# ===================================================================

class TestAutoDirectDelivery:

    def _app(self, adapter):
        app = web.Application()
        app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
        return app

    @pytest.mark.asyncio
    async def test_deliver_only_auto_resolves_and_delivers(self):
        routes = {"notify": _auto_route(deliver_only=True)}
        adapter = _make_adapter(routes)
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM, Platform.DISCORD],
            connected=[Platform.TELEGRAM, Platform.DISCORD],
            homes={Platform.TELEGRAM: "tg-home", Platform.DISCORD: "dc-home"},
        )

        handle_message_calls: list[MessageEvent] = []

        async def _capture(event):
            handle_message_calls.append(event)

        adapter.handle_message = _capture

        async with TestClient(TestServer(self._app(adapter))) as cli:
            resp = await cli.post(
                "/webhooks/notify",
                data=json.dumps({"message": "deploy finished"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": "d-auto-1",
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "delivered"

        await asyncio.sleep(0.05)
        assert handle_message_calls == []
        adapters[Platform.TELEGRAM].send.assert_awaited_once()
        call = adapters[Platform.TELEGRAM].send.await_args
        assert call.args[0] == "tg-home"
        assert call.args[1] == "deploy finished"
        adapters[Platform.DISCORD].send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_deliver_only_auto_falls_back_to_log(self):
        """No qualifying platform → the direct path logs and returns 200."""
        routes = {"notify": _auto_route(deliver_only=True)}
        adapter = _make_adapter(routes)
        adapters = _wire_runner(
            adapter,
            declared=[Platform.TELEGRAM],
            connected=[Platform.TELEGRAM],
            homes={},
        )

        async with TestClient(TestServer(self._app(adapter))) as cli:
            resp = await cli.post(
                "/webhooks/notify",
                data=json.dumps({"message": "hi"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Delivery": "d-auto-2",
                },
            )
            assert resp.status == 200

        adapters[Platform.TELEGRAM].send.assert_not_awaited()


# ===================================================================
# Startup validation
# ===================================================================

class TestAutoStartupValidation:

    @pytest.mark.asyncio
    async def test_deliver_only_with_auto_passes_validation(self):
        """``auto`` is a real target — deliver_only must accept it."""
        routes = {"notify": _auto_route(deliver_only=True)}
        adapter = _make_adapter(routes)
        # connect() does more than validation (binds a socket) — we just
        # want to verify the validation doesn't raise.  Call it and tear
        # down immediately.
        try:
            started = await adapter.connect()
            if started:
                await adapter.disconnect()
        except ValueError:
            pytest.fail("deliver_only + deliver=auto should pass validation")
