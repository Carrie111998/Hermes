"""Telegram connect must not gate the rest of gateway startup.

Gateway death forensics (2026-07-16): a NordVPN/DNS flap made all outbound
name resolution fail (``getaddrinfo failed``). Telegram's boot-time
``connect()`` was awaited inline in the platform-connect loop, so during the
outage every restart attempt blocked on the Telegram connect ladder and never
reached a running state — turning a ~10-minute network blip into a ~27-minute
restart storm (the gateway could not boot until DNS recovered).

The fix backgrounds Telegram's boot-time connect exactly like WhatsApp
(fire-and-retry): the startup loop proceeds while Telegram connects
concurrently, and any failure lands in ``_failed_platforms`` so the existing
reconnect watcher keeps retrying at its normal backoff cadence. The gateway now
comes up (api_server bound, cron + event bus running) during a Telegram/DNS
outage, and Telegram wires itself in when the network returns.

Companion to ``test_whatsapp_background_connect.py``.
"""

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner
from gateway.status import read_runtime_status


class _BlockingTelegramAdapter(BasePlatformAdapter):
    """Simulates a Telegram connect that hangs until the network answers.

    Mirrors the real failure mode: during a DNS outage ``connect()`` blocks on
    the reconnect ladder for the full connect timeout. Here it blocks on an
    event the test never releases inside the assertion window, so if startup
    ever *awaits* it inline the whole ``start()`` hangs (caught by the outer
    ``wait_for`` timeout).
    """

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.connect_started = asyncio.Event()
        self._release = asyncio.Event()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.connect_started.set()
        await self._release.wait()
        return True

    async def disconnect(self) -> None:
        self._release.set()
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _FailingTelegramAdapter(BasePlatformAdapter):
    """Telegram connect that fails fast with a retryable network error."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._set_fatal_error(
            "telegram_network_error",
            "Telegram polling could not reconnect after 10 network error retries.",
            retryable=True,
        )
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _SuccessfulApiAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.API_SERVER)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


async def _wait_for(predicate, timeout: float = 2.0):
    """Poll ``predicate`` cooperatively until true or timeout."""
    deadline = 0.0
    while deadline < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.01)
        deadline += 0.01
    return predicate()


def _make_runner(monkeypatch, tmp_path, tg_adapter):
    """Build a runner whose config lists Telegram *before* api_server."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        # Insertion order matters — Telegram is iterated before api_server, so
        # under the old inline-await code api_server bind waited on Telegram.
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="***"),
            Platform.API_SERVER: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    api_adapter = _SuccessfulApiAdapter()

    def _make(platform, platform_config):
        if platform == Platform.TELEGRAM:
            return tg_adapter
        if platform == Platform.API_SERVER:
            return api_adapter
        return None

    monkeypatch.setattr(runner, "_create_adapter", _make)

    # ``start()`` awaits ``events.gateway_integration.startup`` inline. That does
    # real I/O against the *canonical* ~/.hermes event bus (13 subscribers, the
    # tracker-intent-applier's idempotency rehydrate + jobops :4100 probe) and
    # can block for minutes on a loaded box — wholly unrelated to platform-connect
    # backgrounding. Neutralize it so this test stays hermetic and fast and
    # asserts only the startup-ordering behavior it is about.
    import events.gateway_integration as _ebi

    monkeypatch.setattr(_ebi, "startup", lambda *a, **k: None)
    return runner


@pytest.mark.asyncio
async def test_telegram_connect_does_not_block_api_server_bind(monkeypatch, tmp_path):
    """A hung Telegram connect (DNS outage) must not delay the api_server bind."""
    tg_adapter = _BlockingTelegramAdapter()
    runner = _make_runner(monkeypatch, tmp_path, tg_adapter)

    try:
        # If Telegram were still awaited inline, start() would block on the
        # never-released event and blow this timeout — that's the regression
        # the 2026-07-16 restart storm exposed.
        ok = await asyncio.wait_for(runner.start(), timeout=30)

        assert ok is True
        # api_server came up without waiting for Telegram.
        assert Platform.API_SERVER in runner.adapters
        # Telegram is still connecting in the background, not yet registered.
        assert Platform.TELEGRAM not in runner.adapters
        # ...and its background connect actually fired.
        assert await _wait_for(lambda: tg_adapter.connect_started.is_set())

        state = read_runtime_status()
        assert state["platforms"]["api_server"]["state"] == "connected"
        assert state["platforms"]["telegram"]["state"] == "connecting"
    finally:
        tg_adapter._release.set()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_telegram_background_connect_failure_queues_for_retry(monkeypatch, tmp_path):
    """A failed background Telegram connect lands in the reconnect queue."""
    tg_adapter = _FailingTelegramAdapter()
    runner = _make_runner(monkeypatch, tmp_path, tg_adapter)

    try:
        ok = await asyncio.wait_for(runner.start(), timeout=30)

        assert ok is True
        # api_server still connected despite Telegram failing.
        assert Platform.API_SERVER in runner.adapters
        # Telegram failed in the background and is queued for the reconnect
        # watcher — never registered as connected. Crucially, a retryable
        # telegram fatal during startup no longer aborts the boot.
        assert await _wait_for(lambda: Platform.TELEGRAM in runner._failed_platforms)
        assert Platform.TELEGRAM not in runner.adapters

        state = read_runtime_status()
        assert state["platforms"]["telegram"]["state"] == "retrying"
    finally:
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)
