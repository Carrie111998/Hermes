"""Focused tests for retired Telegram generation ownership."""

import asyncio
import gc
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram import retirement
from plugins.platforms.telegram.adapter import TelegramAdapter


class _Client:
    def __init__(self):
        self.is_closed = False

    def close(self):
        self.is_closed = True


class _Request:
    def __init__(
        self,
        release: asyncio.Event | None = None,
        *,
        label: str = "request.shutdown",
        calls: list[str] | None = None,
    ):
        self.release = release or asyncio.Event()
        self.label = label
        self.calls = calls if calls is not None else []
        self.shutdown_calls = 0
        self.client = _Client()

    async def shutdown(self):
        self.shutdown_calls += 1
        self.calls.append(self.label)
        await self.release.wait()
        self.client.close()


class _Updater:
    def __init__(
        self, release: asyncio.Event | None = None, *, calls: list[str] | None = None
    ):
        self.release = release or asyncio.Event()
        self.calls = calls if calls is not None else []
        self.running = True
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1
        self.calls.append("updater.stop")
        while not self.release.is_set():
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                continue
        self.running = False


class _App:
    def __init__(self, *, release: asyncio.Event | None = None):
        self.release = release or asyncio.Event()
        self.calls: list[str] = []
        self.running = True
        self.updater = _Updater(self.release, calls=self.calls)
        self.polling = _Request(
            self.release, label="polling.shutdown", calls=self.calls
        )
        self.general = _Request(
            self.release, label="general.shutdown", calls=self.calls
        )
        self.bot = SimpleNamespace(_request=(self.polling, self.general))
        self.update_fetcher = asyncio.create_task(
            self.release.wait(), name="Application:update_fetcher"
        )
        self.stop_calls = 0
        self.shutdown_calls = 0

    async def stop(self):
        self.stop_calls += 1
        self.calls.append("app.stop")
        await self.release.wait()
        self.running = False
        await self.update_fetcher

    async def shutdown(self):
        self.shutdown_calls += 1
        self.calls.append("app.shutdown")
        await self.release.wait()


class _ConnectApp(_App):
    def __init__(self, *, release: asyncio.Event):
        super().__init__(release=release)
        self.running = False
        self.add_handler = MagicMock()
        self.start_calls = 0

    async def initialize(self):
        await self.release.wait()

    async def start(self):
        self.start_calls += 1
        self.running = True


def _adapter() -> TelegramAdapter:
    return TelegramAdapter(PlatformConfig(enabled=True, token="retired-test-token"))


@pytest.mark.asyncio
async def test_retired_owner_drains_and_releases_slot(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.2)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None
    assert len(entry.registry.entries) == 1
    release.set()
    await asyncio.wait_for(entry.registry.wait_for_entry(entry, 1.0), 1.0)
    assert entry.state == "CLEANED"
    assert not entry.registry.entries
    assert app.updater.stop_calls == 1
    assert app.stop_calls == 1
    assert app.shutdown_calls == 1
    assert app.calls == [
        "updater.stop",
        "app.stop",
        "app.shutdown",
        "polling.shutdown",
        "general.shutdown",
    ]


@pytest.mark.asyncio
async def test_retired_capacity_blocks_until_old_generation_finishes(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.01)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None
    assert not await entry.registry.wait_for_capacity(0.01)
    release.set()
    assert await entry.registry.wait_for_capacity(2.0)
    assert await entry.registry.wait_for_capacity(0.1)


@pytest.mark.asyncio
async def test_disconnect_captures_application_before_outer_cancellation(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.01)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    adapter._app = app
    adapter._bot = app.bot
    monkeypatch.setattr(adapter, "_release_platform_lock", lambda: None)
    task = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    registry = adapter._retirement_registry()
    assert registry.entries
    release.set()
    await asyncio.sleep(0.1)
    assert not registry.entries


@pytest.mark.asyncio
async def test_retirement_is_idempotent_for_same_application():
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    first = adapter._retire_app(app, start=True)
    second = adapter._retire_app(app, start=True)
    assert first is second
    assert len(first.registry.entries) == 1
    release.set()
    await asyncio.wait_for(first.registry.wait_for_entry(first, 1.0), 1.0)


@pytest.mark.asyncio
async def test_stale_generation_progress_cannot_mutate_current_adapter():
    adapter = _adapter()
    adapter._polling_generation = 2
    adapter._polling_progress_accepting = True
    adapter._polling_progress_event = asyncio.Event()
    adapter._record_polling_progress(1)
    assert not adapter._polling_progress_event.is_set()


@pytest.mark.asyncio
async def test_timed_out_child_remains_owned_and_retries_after_release(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.01)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.01)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None
    await asyncio.sleep(0.05)
    assert entry in entry.registry.entries
    assert entry.state != "CLEANED"
    assert entry.active_tasks
    assert not await entry.registry.wait_for_capacity(0.01)
    release.set()
    assert await entry.registry.wait_for_capacity(1.0)
    assert not entry.registry.entries


@pytest.mark.asyncio
async def test_cancelled_cleanup_carrier_does_not_drop_owner(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.05)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None and entry.cleanup_task is not None
    entry.cleanup_task.cancel()
    await asyncio.sleep(0.02)
    assert entry in entry.registry.entries
    release.set()
    assert await entry.registry.wait_for_capacity(1.0)
    assert not entry.registry.entries


@pytest.mark.asyncio
async def test_real_connect_gate_refuses_new_application_at_capacity(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_RETIRED_CAPACITY_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr(retirement, "CLEANUP_RETRY_DELAY", 0.001)
    owner = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = owner._retire_app(app, start=True)
    candidate = _adapter()
    result = await candidate.connect(is_reconnect=True)
    assert result is False
    assert candidate._app is None
    assert candidate.has_fatal_error
    assert candidate.fatal_error_code == "telegram_retired_generation_capacity"
    release.set()
    assert await entry.registry.wait_for_capacity(1.0)


@pytest.mark.asyncio
async def test_connect_rebuild_serializes_two_consecutive_abandonments(monkeypatch):
    """The internal retry ladder cannot build B/C over an occupied A/B slot."""
    import plugins.platforms.telegram.adapter as module

    release_a = asyncio.Event()
    release_b = asyncio.Event()
    app_a = _ConnectApp(release=release_a)
    app_b = _ConnectApp(release=release_b)
    generations = [app_a, app_b]
    generation_c_created = False

    def _build_generation():
        nonlocal generation_c_created
        if generations:
            return generations.pop(0)
        generation_c_created = True
        raise AssertionError("generation C must not be constructed")

    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.get_updates_request.return_value = builder
    builder.build.side_effect = _build_generation
    monkeypatch.setattr(
        module,
        "Application",
        SimpleNamespace(builder=MagicMock(return_value=builder)),
    )
    monkeypatch.setattr(module, "HTTPXRequest", lambda **_kwargs: MagicMock())
    monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "true")
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda _scope, _identity, metadata=None: (True, None),
    )
    monkeypatch.setattr(module, "_RETIRED_CAPACITY_WAIT_TIMEOUT", 0.2)
    monkeypatch.setattr(retirement, "CLEANUP_RETRY_DELAY", 0.001)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())
    deadline_calls = 0

    async def _abandon_initialize(awaitable, timeout, **_kwargs):
        nonlocal deadline_calls
        del timeout
        deadline_calls += 1
        await real_sleep(0)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(module, "_await_with_thread_deadline", _abandon_initialize)

    capacity_waits: list[str] = []
    original_wait = retirement.TelegramRetirementRegistry.wait_for_capacity

    async def _observed_wait(registry, timeout=retirement.CAPACITY_WAIT_TIMEOUT):
        owned_a = registry.find(app_a)
        owned_b = registry.find(app_b)
        if owned_a is not None:
            capacity_waits.append("A")
            assert builder.build.call_count == 1
            assert owned_a.active_tasks
            assert owned_a.app is app_a
            release_a.set()
        elif owned_b is not None:
            capacity_waits.append("B")
            assert builder.build.call_count == 2
            assert owned_b.active_tasks
            assert owned_b.app is app_b
        return await original_wait(registry, timeout)

    monkeypatch.setattr(
        retirement.TelegramRetirementRegistry,
        "wait_for_capacity",
        _observed_wait,
    )

    adapter = _adapter()
    adapter._wire_plugin_handlers = MagicMock()
    adapter._register_handlers = MagicMock()
    assert await adapter.connect(is_reconnect=True) is False

    assert deadline_calls == 2
    assert capacity_waits == ["A", "B"]
    assert builder.build.call_count == 2
    assert generation_c_created is False
    registry_b = adapter._retirement_registry(create=False)
    assert registry_b is not None
    owned_b = registry_b.find(app_b)
    assert owned_b is not None
    assert owned_b.active_tasks
    assert owned_b.updater is app_b.updater
    assert owned_b.bot is app_b.bot
    assert owned_b.requests == (app_b.polling, app_b.general)

    release_b.set()
    assert await module.drain_telegram_retired_generations(timeout=1.0)
    assert retirement.registry_count() == 0
    assert app_a.update_fetcher.done()
    assert app_b.update_fetcher.done()
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and task.get_name().startswith("telegram-retired-cleanup:")
    ]
    assert owned_b.app is None
    assert owned_b.updater is None
    assert owned_b.bot is None
    assert owned_b.requests == ()


@pytest.mark.asyncio
async def test_fatal_handoff_has_one_total_capacity_deadline(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_RETIRED_CAPACITY_WAIT_TIMEOUT", 0.01)
    owner = _adapter()
    held_release = asyncio.Event()
    held_app = _App(release=held_release)
    held_entry = owner._retire_app(held_app, start=True)
    assert held_entry is not None

    candidate = _adapter()
    candidate_release = asyncio.Event()
    candidate_app = _App(release=candidate_release)
    candidate._app = candidate_app
    candidate._bot = candidate_app.bot
    candidate._notify_fatal_error = AsyncMock()
    real_sleep = asyncio.sleep
    monkeypatch.setattr(module.asyncio, "sleep", AsyncMock())

    async def _abandon_stop(_awaitable, timeout, **_kwargs):
        del timeout
        await real_sleep(0)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(module, "_await_with_thread_deadline", _abandon_stop)

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(module.RetirementCapacityError):
        await candidate._handle_polling_network_error(
            OSError("bounded fatal handoff test")
        )
    assert loop.time() - started < 0.1
    candidate._notify_fatal_error.assert_not_awaited()
    assert candidate._app is candidate_app
    assert candidate._background_tasks

    held_release.set()
    assert await held_entry.registry.wait_for_capacity(1.0)
    candidate_release.set()
    await asyncio.gather(*tuple(candidate._background_tasks), return_exceptions=True)
    candidate_entry = candidate._retire_app(candidate_app, start=True)
    assert candidate_entry is not None
    assert await module.drain_telegram_retired_generations(timeout=1.0)


@pytest.mark.asyncio
async def test_global_shutdown_drains_after_adapter_removal(monkeypatch, recwarn):
    import plugins.platforms.telegram.adapter as module
    adapter = _adapter()
    release = asyncio.Event()
    release.set()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None
    registry = entry.registry
    assert len(registry.entries) == 1
    adapter._app = None
    adapter._bot = None
    adapter_ref = weakref.ref(adapter)
    del adapter
    gc.collect()
    assert adapter_ref() is None

    assert await asyncio.wait_for(module.drain_telegram_retired_generations(), 1.0)
    assert not registry.entries
    assert entry.state == "CLEANED"
    assert entry.app is None
    assert entry.updater is None
    assert entry.bot is None
    assert entry.requests == ()
    assert entry.cleanup_task is not None and entry.cleanup_task.done()
    assert app.update_fetcher.done()
    assert not app.updater.running
    assert app.polling.client.is_closed
    assert app.general.client.is_closed
    assert not [
        task
        for task in asyncio.all_tasks()
        if task is not asyncio.current_task()
        and not task.done()
        and (
            task.get_name().startswith("telegram-retired-cleanup:")
            or task.get_name() == "Application:update_fetcher"
        )
    ]
    bad_warnings = [
        warning
        for warning in recwarn
        if any(
            marker in str(warning.message).lower()
            for marker in ("unclosed", "destroyed but pending", "transport")
        )
    ]
    assert not bad_warnings


@pytest.mark.asyncio
async def test_global_shutdown_drain_is_idempotent_and_bounded(monkeypatch):
    import plugins.platforms.telegram.adapter as module
    from gateway.run import _drain_global_telegram_retired_generations

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.05)
    adapter = _adapter()
    release = asyncio.Event()
    app = _App(release=release)
    entry = adapter._retire_app(app, start=True)
    assert entry is not None

    async def _release_cleanup():
        await asyncio.sleep(0.01)
        release.set()

    release_task = asyncio.create_task(_release_cleanup())
    first, second = await asyncio.gather(
        _drain_global_telegram_retired_generations(),
        _drain_global_telegram_retired_generations(),
    )
    await release_task

    assert first is True
    assert second is True
    assert not entry.registry.entries
    assert app.updater.stop_calls == 1
    assert app.stop_calls == 1
    assert app.shutdown_calls == 1
    assert app.polling.shutdown_calls == 1
    assert app.general.shutdown_calls == 1


@pytest.mark.asyncio
async def test_global_shutdown_drain_repeats_without_retention(monkeypatch):
    import plugins.platforms.telegram.adapter as module
    from gateway.run import _drain_global_telegram_retired_generations

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.05)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.05)
    assert retirement.registry_count() == 0
    for cycle in range(32):
        adapter = TelegramAdapter(
            PlatformConfig(enabled=True, token=f"retired-repeat-{cycle}")
        )
        release = asyncio.Event()
        release.set()
        app = _App(release=release)
        entry = adapter._retire_app(app, start=True)
        assert entry is not None
        del adapter

        assert await _drain_global_telegram_retired_generations()
        assert not entry.registry.entries
        assert entry.app is None
        assert entry.updater is None
        assert entry.bot is None
        assert entry.requests == ()
        assert app.update_fetcher.done()
        assert app.polling.client.is_closed
        assert app.general.client.is_closed
        assert retirement.registry_count() == 0


def test_retirement_registry_does_not_retain_closed_loop():
    loop = asyncio.new_event_loop()
    loop_ref = weakref.ref(loop)

    async def _exercise() -> None:
        adapter = _adapter()
        release = asyncio.Event()
        release.set()
        app = _App(release=release)
        entry = adapter._retire_app(app, start=True)
        assert entry is not None
        assert await retirement.drain_retired_generations(timeout=1.0)
        await asyncio.sleep(0)
        assert retirement.registry_count() == 0

    loop.run_until_complete(_exercise())
    loop.close()
    del loop
    gc.collect()
    assert loop_ref() is None


@pytest.mark.asyncio
async def test_global_shutdown_drain_uses_one_total_budget(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "_UPDATER_STOP_TIMEOUT", 0.2)
    monkeypatch.setattr(module, "_DISCONNECT_STEP_TIMEOUT", 0.2)
    releases = [asyncio.Event(), asyncio.Event()]
    entries = []
    apps = []
    for index, release in enumerate(releases):
        adapter = TelegramAdapter(
            PlatformConfig(enabled=True, token=f"retired-budget-{index}")
        )
        app = _App(release=release)
        entry = adapter._retire_app(app, start=True)
        assert entry is not None
        entries.append(entry)
        apps.append(app)

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert not await module.drain_telegram_retired_generations(timeout=0.02)
    elapsed = loop.time() - started
    assert elapsed < 0.1
    assert all(entry.registry.entries for entry in entries)

    for release in releases:
        release.set()
    assert await module.drain_telegram_retired_generations(timeout=1.0)
    assert all(not entry.registry.entries for entry in entries)
    assert all(app.update_fetcher.done() for app in apps)
    assert all(app.polling.client.is_closed for app in apps)
    assert all(app.general.client.is_closed for app in apps)
