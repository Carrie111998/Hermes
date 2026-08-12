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
from tests.gateway.hang_guards import HANG_GUARD_S


class _BlockingTelegramAdapter(BasePlatformAdapter):
    """Simulates a Telegram connect that hangs until the network answers.

    Mirrors the real failure mode: during a DNS outage ``connect()`` blocks on
    the reconnect ladder for the full connect timeout. Here it parks on an
    event the test never releases inside the assertion window, so the connect
    is *demonstrably still outstanding* while the rest of startup is checked.

    ``connect_started`` / ``connect_returned`` are the ordering primitives: the
    first says the connect was entered, the second stays ``False`` for as long
    as it is parked. Together they let the test state the invariant directly
    ("api_server bound while the Telegram connect had not returned") instead of
    inferring it from how long ``start()`` took.
    """

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.connect_started = asyncio.Event()
        self.connect_returned = False
        self._release = asyncio.Event()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.connect_started.set()
        await self._release.wait()
        self.connect_returned = True
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
        self.connect_started = asyncio.Event()
        self.connect_returned = False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self.connect_started.set()
        self._set_fatal_error(
            "telegram_network_error",
            "Telegram polling could not reconnect after 10 network error retries.",
            retryable=True,
        )
        self.connect_returned = True
        return False

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _SuccessfulApiAdapter(BasePlatformAdapter):
    """Stands in for the :8642 bind, and records the ordering as it happens.

    ``bound`` is the barrier the test waits on instead of a wall clock, and
    ``telegram_connect_outstanding_at_bind`` snapshots — at the instant of the
    bind — whether the Telegram connect had returned yet. That snapshot IS the
    regression assertion: under the old inline-await code the bind could only
    run *after* Telegram's connect resolved.
    """

    def __init__(self, gate=None):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.API_SERVER)
        self.bound = asyncio.Event()
        self.telegram_connect_outstanding_at_bind = None
        self._gate = gate

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self._gate is not None:
            self.telegram_connect_outstanding_at_bind = not self._gate.connect_returned
        self.bound.set()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise NotImplementedError

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _SignalOnQueue(dict):
    """``_failed_platforms`` that announces the insertion under test.

    The runner reaches this dict by more than one route — the background
    connect's own bookkeeping and the adapter fatal-error handler — so the
    barrier belongs on the dict itself rather than on one caller. Wrapping a
    single method fires only if that route happens to win, which on
    2026-08-12 turned a 13s test into a 34s one waiting for the reconnect
    watcher to come around.

    ``setdefault`` is overridden explicitly: CPython's C implementation does
    not route through ``__setitem__``.
    """

    def __init__(self, platform, event):
        super().__init__()
        self._platform = platform
        self._event = event

    def _signal(self, key):
        if key == self._platform:
            self._event.set()

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._signal(key)

    def setdefault(self, key, default=None):
        result = super().setdefault(key, default)
        self._signal(key)
        return result


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
    api_adapter = _SuccessfulApiAdapter(gate=tg_adapter)

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

    # Same treatment for the post-connect channel-directory build. It walks the
    # platform plugin registry, which imports every platform plugin module (18
    # vendor SDKs on a cold bytecode cache) — measured at ~15s of a ~17s
    # ``start()`` on 2026-08-12, i.e. the dominant cost in this test and
    # entirely downstream of the connect ordering it asserts. Neutralizing it
    # keeps the remaining bound on ``start()`` an honest hang guard instead of
    # a race against unrelated import time.
    import gateway.channel_directory as _chdir

    async def _no_directory(_adapters):
        return {"platforms": {}}

    monkeypatch.setattr(_chdir, "build_channel_directory", _no_directory)
    return runner, api_adapter


@pytest.mark.asyncio
async def test_telegram_connect_does_not_block_api_server_bind(monkeypatch, tmp_path):
    """A hung Telegram connect (DNS outage) must not delay the api_server bind."""
    tg_adapter = _BlockingTelegramAdapter()
    runner, api_adapter = _make_runner(monkeypatch, tmp_path, tg_adapter)

    # The invariant is an *ordering*, not a duration: api_server must bind
    # while the Telegram connect is still outstanding. So park the connect on
    # an event this block never releases and wait on the two barriers the code
    # itself raises — the connect being entered, and the bind completing. If
    # Telegram were still awaited inline the bind is never reached at all, so
    # ``api_adapter.bound`` never fires; HANG_GUARD_S bounds only that genuine
    # deadlock, and every assertion below is about state, not elapsed time.
    start_task = asyncio.create_task(runner.start())
    try:
        await asyncio.wait_for(tg_adapter.connect_started.wait(), timeout=HANG_GUARD_S)
        try:
            await asyncio.wait_for(api_adapter.bound.wait(), timeout=HANG_GUARD_S)
        except asyncio.TimeoutError:
            raise AssertionError(
                "api_server never bound while the Telegram connect was parked "
                "— the boot-time connect is gating startup again"
            ) from None

        # Recorded at the instant of the bind: Telegram's connect had not
        # returned. That is the 2026-07-16 regression stated directly.
        assert api_adapter.telegram_connect_outstanding_at_bind is True
        assert tg_adapter.connect_returned is False

        # Startup as a whole then completes with that connect still parked —
        # the remaining state assertions read a settled runner rather than
        # racing the registration that follows the bind.
        ok = await asyncio.wait_for(start_task, timeout=HANG_GUARD_S)
        assert ok is True
        assert tg_adapter.connect_returned is False

        # api_server came up without waiting for Telegram.
        assert Platform.API_SERVER in runner.adapters
        # Telegram is still connecting in the background, not yet registered.
        assert Platform.TELEGRAM not in runner.adapters

        state = read_runtime_status()
        assert state["platforms"]["api_server"]["state"] == "connected"
        assert state["platforms"]["telegram"]["state"] == "connecting"
    finally:
        start_task.cancel()
        tg_adapter._release.set()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_telegram_background_connect_failure_queues_for_retry(monkeypatch, tmp_path):
    """A failed background Telegram connect lands in the reconnect queue."""
    tg_adapter = _FailingTelegramAdapter()
    runner, api_adapter = _make_runner(monkeypatch, tmp_path, tg_adapter)

    # The state transition under test is "the failure got queued for the
    # reconnect watcher". Watching the queue itself turns that into an
    # awaitable signal — an ordering primitive — instead of a poll loop or an
    # elapsed-time bound.
    queued = asyncio.Event()
    monkeypatch.setattr(
        runner, "_failed_platforms", _SignalOnQueue(Platform.TELEGRAM, queued)
    )

    start_task = asyncio.create_task(runner.start())
    try:
        # Barrier 1 — the api_server bind happened.
        await asyncio.wait_for(api_adapter.bound.wait(), timeout=HANG_GUARD_S)

        # Barrier 2 — startup completes; a retryable Telegram fatal raised in
        # the background no longer aborts the boot.
        ok = await asyncio.wait_for(start_task, timeout=HANG_GUARD_S)
        assert ok is True
        # api_server still connected despite Telegram failing.
        assert Platform.API_SERVER in runner.adapters

        # Barrier 3 — the queueing itself. Deliberately NOT a wait on the
        # background task: ``_background_tasks`` is a live set whose members
        # come and go (the connect task is discarded on completion and
        # long-lived supervised watchers take its place), so a snapshot of it
        # is not a signal about this connect — waiting on one hangs the test.
        await asyncio.wait_for(queued.wait(), timeout=HANG_GUARD_S)
        assert tg_adapter.connect_returned is True
        # Telegram failed in the background and is queued for the reconnect
        # watcher — never registered as connected.
        assert Platform.TELEGRAM in runner._failed_platforms
        assert Platform.TELEGRAM not in runner.adapters

        state = read_runtime_status()
        assert state["platforms"]["telegram"]["state"] == "retrying"
    finally:
        start_task.cancel()
        for task in list(runner._background_tasks):
            task.cancel()
        await asyncio.sleep(0)
