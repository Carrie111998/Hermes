"""WhatsApp connect must not gate the rest of gateway startup.

Boot forensics (2026-07-10): the platform-connect loop awaited WhatsApp's
``connect()`` inline, and with the WhatsApp bridge (localhost:3000) down that
call blocked for the full 30s platform-connect timeout on *every* boot. Only
after that 30s did the loop reach ``api_server`` and bind :8642 — so every
gateway restart and every watchdog recovery paid ~30s of dead time before the
HTTP API came up.

The fix fires WhatsApp's boot-time connect in the background (fire-and-retry):
the startup loop proceeds straight to ``api_server`` while WhatsApp connects
concurrently, and any failure lands in ``_failed_platforms`` so the existing
reconnect watcher keeps retrying at its normal backoff cadence.
"""

import asyncio

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run import GatewayRunner
from gateway.status import read_runtime_status


class _BlockingWhatsAppAdapter(BasePlatformAdapter):
    """Simulates a WhatsApp connect that hangs until its bridge answers.

    Mirrors the real failure mode: ``connect()`` blocks for the full connect
    timeout when the bridge is down. Here it blocks on an event the test never
    releases inside the assertion window, so if startup ever *awaits* it inline
    the whole ``start()`` hangs (caught by the outer ``wait_for`` timeout).
    """

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.WHATSAPP)
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


class _FailingWhatsAppAdapter(BasePlatformAdapter):
    """WhatsApp connect that fails fast with a retryable error."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.WHATSAPP)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._set_fatal_error(
            "whatsapp_connect_error",
            "whatsapp connect timed out after 30s",
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


def _make_runner(monkeypatch, tmp_path, wa_adapter):
    """Build a runner whose config lists WhatsApp *before* api_server."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(
        # Insertion order matters — WhatsApp is iterated before api_server, so
        # under the old inline-await code api_server bind waited on WhatsApp.
        platforms={
            Platform.WHATSAPP: PlatformConfig(enabled=True, token="***"),
            Platform.API_SERVER: PlatformConfig(enabled=True, token="***"),
        },
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    api_adapter = _SuccessfulApiAdapter()

    def _make(platform, platform_config):
        if platform == Platform.WHATSAPP:
            return wa_adapter
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
async def test_whatsapp_connect_does_not_block_api_server_bind(monkeypatch, tmp_path):
    """A hung WhatsApp connect must not delay the api_server bind."""
    wa_adapter = _BlockingWhatsAppAdapter()
    runner = _make_runner(monkeypatch, tmp_path, wa_adapter)

    try:
        # If WhatsApp were still awaited inline, start() would block on the
        # never-released event and blow this timeout — that's the regression.
        # 30s (matching the companion Telegram test) leaves headroom for the
        # rest of start()'s post-connect housekeeping — the first-invocation
        # lazy imports, channel-directory build, and restart-notification
        # checks — on a memory-pressured box.
        ok = await asyncio.wait_for(runner.start(), timeout=30)

        assert ok is True
        # api_server came up without waiting for WhatsApp.
        assert Platform.API_SERVER in runner.adapters
        # WhatsApp is still connecting in the background, not yet registered.
        assert Platform.WHATSAPP not in runner.adapters
        # ...and its background connect actually fired.
        assert await _wait_for(lambda: wa_adapter.connect_started.is_set())

        state = read_runtime_status()
        assert state["platforms"]["api_server"]["state"] == "connected"
        assert state["platforms"]["whatsapp"]["state"] == "connecting"
    finally:
        wa_adapter._release.set()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_whatsapp_background_connect_failure_queues_for_retry(monkeypatch, tmp_path):
    """A failed background WhatsApp connect lands in the reconnect queue."""
    wa_adapter = _FailingWhatsAppAdapter()
    runner = _make_runner(monkeypatch, tmp_path, wa_adapter)

    try:
        ok = await asyncio.wait_for(runner.start(), timeout=30)

        assert ok is True
        # api_server still connected despite WhatsApp failing.
        assert Platform.API_SERVER in runner.adapters
        # WhatsApp failed in the background and is queued for the reconnect
        # watcher — never registered as connected.
        assert await _wait_for(lambda: Platform.WHATSAPP in runner._failed_platforms)
        assert Platform.WHATSAPP not in runner.adapters

        state = read_runtime_status()
        assert state["platforms"]["whatsapp"]["state"] == "retrying"
    finally:
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)
