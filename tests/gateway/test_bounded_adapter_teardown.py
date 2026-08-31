"""Regression tests: the shutdown teardown loop must not hang on a wedged adapter.

`GatewayRunner._stop_impl()` tears down every adapter by awaiting
`cancel_background_tasks()` then `disconnect()`. Both calls can block
indefinitely when a platform's network state is half-dead (e.g. a wedged
Feishu/Lark WebSocket thread waiting on I/O). An unbounded await stalls the
whole shutdown past systemd's TimeoutStopSec; the resulting SIGKILL skips
atexit PID-file cleanup, so the next start dies with "PID file race lost"
(#14128).

The fix routes both teardown loops through `_bounded_adapter_teardown`,
which wraps each await in the existing per-adapter timeout budget
(HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT) and always returns.
"""

import asyncio
import concurrent.futures
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.run import GatewayRunner
from plugins.platforms.feishu.adapter import FeishuAdapter


@pytest.fixture
def bare_runner():
    """A GatewayRunner shell that only needs _bounded_adapter_teardown."""
    return object.__new__(GatewayRunner)


async def _eventually_ws_worker_stops(
    stopped: threading.Event,
    worker_future: asyncio.Future,
    *,
    timeout: float,
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if stopped.is_set() and worker_future.done():
            return True
        await asyncio.sleep(0.01)
    return stopped.is_set() and worker_future.done()


@pytest.mark.asyncio
async def test_teardown_bounds_hanging_cancel(bare_runner, monkeypatch, caplog):
    """A wedged cancel_background_tasks() must time out, then disconnect runs."""
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    adapter = MagicMock()

    async def hang():
        await asyncio.sleep(0.2)

    adapter.cancel_background_tasks = AsyncMock(side_effect=hang)
    adapter.disconnect = AsyncMock(return_value=None)

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        await asyncio.wait_for(
            bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU),
            timeout=5.0,
        )

    assert "feishu background-task cancel timed out" in caplog.text
    # disconnect still attempted after the cancel timeout — forward progress.
    adapter.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_continues_after_cancellation_swallowing_background_cancel(
    bare_runner, monkeypatch, caplog
):
    """A stuck cancellation handler cannot prevent adapter disconnect.

    This models a platform task that catches ``CancelledError`` while it is
    unwinding.  The teardown deadline must release runner ownership promptly,
    then proceed to disconnect instead of waiting for that old task forever.
    """
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.01")
    adapter = MagicMock()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def swallow_cancellation():
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        finished.set()

    adapter.cancel_background_tasks = AsyncMock(side_effect=swallow_cancellation)
    adapter.disconnect = AsyncMock(return_value=None)
    operation = asyncio.create_task(
        bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)
    )
    await started.wait()
    done, _pending = await asyncio.wait({operation}, timeout=0.2)
    try:
        assert operation in done
        adapter.disconnect.assert_awaited_once()
        assert "feishu background-task cancel timed out" in caplog.text
    finally:
        release.set()
        await asyncio.wait({operation}, timeout=0.2)
        await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_feishu_timeout_reaps_ws_default_executor_worker(
    bare_runner, monkeypatch
):
    """An outer disconnect deadline must not orphan Feishu's WS worker.

    The gateway event loop historically owned ``_ws_future`` through its
    default executor.  If cancellation landed while Feishu awaited the CLOSE
    acknowledgement, ``asyncio.run()`` later hung in
    ``shutdown_default_executor()`` waiting for this worker forever.
    """
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.05")
    adapter = FeishuAdapter(PlatformConfig())
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)

    ws_loop = asyncio.new_event_loop()
    ws_started = threading.Event()
    ws_stopped = threading.Event()

    def run_ws_loop() -> None:
        asyncio.set_event_loop(ws_loop)
        ws_started.set()
        try:
            ws_loop.run_forever()
        finally:
            try:
                pending = [
                    task for task in asyncio.all_tasks(ws_loop) if not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    # Multiple hard-stop callbacks can still be queued. One
                    # bounded turn delivers cancellation without letting a
                    # later loop.stop() break run_until_complete().
                    ws_loop.call_soon(ws_loop.stop)
                    ws_loop.run_forever()
            finally:
                ws_loop.close()
                ws_stopped.set()

    gateway_loop = asyncio.get_running_loop()
    ws_future = gateway_loop.run_in_executor(None, run_ws_loop)
    assert ws_started.wait(timeout=1.0)

    close_started = threading.Event()

    async def never_ack_close() -> None:
        close_started.set()
        await asyncio.Future()

    adapter._ws_client = SimpleNamespace(
        _disconnect=never_ack_close,
        _auto_reconnect=True,
    )
    adapter._ws_thread_loop = ws_loop
    adapter._ws_future = ws_future

    try:
        await bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)

        assert close_started.is_set()
        assert await _eventually_ws_worker_stops(
            ws_stopped, ws_future, timeout=0.5
        ), (
            "gateway teardown returned while Feishu's default-executor WS "
            "worker was still alive; shutdown_default_executor would block"
        )
    finally:
        # Keep the old implementation's expected red result from leaking a
        # live executor worker into the rest of the test process.
        if not ws_stopped.is_set() and not ws_loop.is_closed():
            ws_loop.call_soon_threadsafe(ws_loop.stop)
        assert await _eventually_ws_worker_stops(
            ws_stopped, ws_future, timeout=1.0
        )


@pytest.mark.asyncio
async def test_feishu_ws_worker_does_not_use_loop_default_executor(monkeypatch):
    """The permanent WS loop must not become an asyncio.run exit dependency."""
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    gateway_loop = asyncio.get_running_loop()

    class GatewayLoopSpy:
        def is_closed(self) -> bool:
            return False

        def run_in_executor(self, *_args, **_kwargs):
            pytest.fail("Feishu WS worker must not use the loop default executor")

    adapter._loop = GatewayLoopSpy()
    adapter._hydrate_bot_identity = AsyncMock()
    adapter._build_lark_client = MagicMock(return_value=SimpleNamespace())
    adapter._build_event_handler = MagicMock(return_value=object())

    started = threading.Event()
    release = threading.Event()

    def blocking_ws_worker(*_args) -> None:
        started.set()
        release.wait(timeout=2.0)

    class FakeWSClient:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(feishu_module, "FEISHU_WEBSOCKET_AVAILABLE", True)
    monkeypatch.setattr(feishu_module, "FEISHU_DOMAIN", object())
    monkeypatch.setattr(
        feishu_module,
        "lark",
        SimpleNamespace(LogLevel=SimpleNamespace(INFO="INFO")),
    )
    monkeypatch.setattr(feishu_module, "FeishuWSClient", FakeWSClient)
    monkeypatch.setattr(
        feishu_module,
        "_run_official_feishu_ws_client",
        blocking_ws_worker,
    )

    try:
        await adapter._connect_websocket()
        deadline = gateway_loop.time() + 1.0
        while not started.is_set() and gateway_loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert started.is_set()
        runtime = getattr(adapter, "_ws_runtime", None)
        assert runtime is not None
        assert runtime.thread is not None and runtime.thread.daemon
    finally:
        release.set()
        ws_future = adapter._ws_future
        if ws_future is not None:
            await asyncio.wait_for(asyncio.shield(ws_future), timeout=1.0)
        runtime = getattr(adapter, "_ws_runtime", None)
        thread = getattr(runtime, "thread", None)
        deadline = gateway_loop.time() + 1.0
        while thread is not None and thread.is_alive() and gateway_loop.time() < deadline:
            await asyncio.sleep(0.01)
        assert thread is None or not thread.is_alive()


@pytest.mark.asyncio
async def test_feishu_close_timeout_does_not_cancel_cross_loop_future(monkeypatch):
    """A CLOSE timeout must not callback into an already-closed WS loop.

    ``asyncio.wait_for(asyncio.wrap_future(...))`` cancels the underlying
    concurrent future on timeout.  The future returned by
    ``run_coroutine_threadsafe`` then tries to forward that cancellation to
    its source loop, which may already have been closed by the independently
    armed hard-stop timer.
    """
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)
    monkeypatch.setattr(feishu_module, "_FEISHU_WS_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(feishu_module, "_arm_feishu_ws_loop_hard_stop", lambda *_args: None)
    monkeypatch.setattr(feishu_module, "_request_feishu_ws_loop_stop", lambda *_args: None)

    class CloseFuture(concurrent.futures.Future):
        cancel_requested = False

        def cancel(self) -> bool:
            self.cancel_requested = True
            return super().cancel()

    close_future = CloseFuture()

    def fake_run_coroutine_threadsafe(coro, _loop):
        coro.close()
        return close_future

    monkeypatch.setattr(
        feishu_module.asyncio,
        "run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe,
    )
    adapter._ws_client = SimpleNamespace(
        _disconnect=lambda: asyncio.sleep(0),
        _auto_reconnect=True,
    )
    adapter._ws_thread_loop = SimpleNamespace(is_closed=lambda: False)

    await adapter.disconnect()

    assert not close_future.cancel_requested


@pytest.mark.asyncio
async def test_overlapping_feishu_disconnects_keep_reconnect_closed(monkeypatch):
    """A second teardown must not reopen connect while the first is pending."""
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)
    monkeypatch.setattr(
        feishu_module,
        "_run_official_feishu_ws_client",
        lambda *_args: None,
    )

    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    first_task = None

    async def controlled_cancel(_tasks) -> None:
        if asyncio.current_task() is first_task:
            first_entered.set()
            await release_first.wait()

    monkeypatch.setattr(adapter, "_cancel_pending_tasks", controlled_cancel)
    first_task = asyncio.create_task(adapter.disconnect())
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    second_task = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)

    try:
        assert adapter._ws_disconnect_count == 2
        with pytest.raises(
            RuntimeError,
            match="disconnect is still in progress",
        ):
            adapter._start_ws_runtime(SimpleNamespace())
    finally:
        release_first.set()
        await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=1.0,
        )

    assert adapter._ws_disconnect_count == 0


def test_pending_feishu_ws_runtime_blocks_a_second_start(monkeypatch):
    """Published-but-not-started ownership must still block another worker."""
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    pending = feishu_module._FeishuWSRuntime(client=SimpleNamespace())
    pending.thread = SimpleNamespace(is_alive=lambda: False)
    adapter._ws_runtime = pending

    def forbidden_thread(*_args, **_kwargs):
        pytest.fail("a second websocket thread was constructed")

    monkeypatch.setattr(feishu_module.threading, "Thread", forbidden_thread)
    with pytest.raises(
        RuntimeError,
        match="Previous Feishu websocket worker is still stopping",
    ):
        adapter._start_ws_runtime(SimpleNamespace())


@pytest.mark.asyncio
async def test_gateway_cancel_reaps_new_feishu_ws_runtime(
    bare_runner, monkeypatch
):
    """The production runtime path must survive cancellation during CLOSE."""
    import plugins.platforms.feishu.adapter as feishu_module

    assert feishu_module._load_lark_oapi()
    monkeypatch.setenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "0.05")
    adapter = FeishuAdapter(PlatformConfig())
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)

    started = threading.Event()
    close_started = threading.Event()

    class FakeClient:
        _auto_reconnect = True

        def start(self) -> None:
            import lark_oapi.ws.client as ws_client_module

            started.set()
            ws_client_module.loop.run_forever()

        async def _disconnect(self) -> None:
            close_started.set()
            await asyncio.Future()

    gateway_loop = asyncio.get_running_loop()
    executor_before = getattr(gateway_loop, "_default_executor", None)
    runtime = adapter._start_ws_runtime(FakeClient())
    deadline = gateway_loop.time() + 1.0
    while not started.is_set() and gateway_loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert started.is_set() and runtime.loop_ready.is_set()

    await bare_runner._bounded_adapter_teardown(adapter, Platform.FEISHU)

    deadline = gateway_loop.time() + 1.0
    while not runtime.stopped.is_set() and gateway_loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert close_started.is_set()
    assert runtime.stopped.is_set()
    assert runtime.thread is not None and not runtime.thread.is_alive()
    assert getattr(gateway_loop, "_default_executor", None) is executor_before


@pytest.mark.asyncio
async def test_stubborn_batch_cancel_cannot_retain_feishu_ws_worker(monkeypatch):
    """WS stop is armed before awaiting a child that swallows cancellation."""
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)

    ws_loop = asyncio.new_event_loop()
    ws_started = threading.Event()
    ws_stopped = threading.Event()
    runtime = feishu_module._FeishuWSRuntime(client=SimpleNamespace())

    def run_ws_loop() -> None:
        asyncio.set_event_loop(ws_loop)
        with runtime.lock:
            runtime.loop = ws_loop
        runtime.loop_ready.set()
        ws_started.set()
        try:
            ws_loop.run_forever()
        finally:
            ws_loop.close()
            with runtime.lock:
                runtime.loop = None
            runtime.stopped.set()
            ws_stopped.set()

    thread = threading.Thread(target=run_ws_loop, daemon=True)
    runtime.thread = thread
    adapter._ws_runtime = runtime
    adapter._ws_thread = thread
    adapter._ws_client = runtime.client
    thread.start()
    assert ws_started.wait(timeout=1.0)

    release_child = asyncio.Event()
    child_cancelled = asyncio.Event()

    async def stubborn_child() -> None:
        while not release_child.is_set():
            try:
                await release_child.wait()
            except asyncio.CancelledError:
                child_cancelled.set()

    child = asyncio.create_task(stubborn_child())
    adapter._pending_text_batch_tasks["stubborn"] = child
    operation = asyncio.create_task(adapter.disconnect())
    await asyncio.wait_for(child_cancelled.wait(), timeout=1.0)
    operation.cancel()

    try:
        deadline = asyncio.get_running_loop().time() + 1.0
        while not ws_stopped.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert ws_stopped.is_set()
        assert runtime.stopped.is_set()
        assert not operation.done()
    finally:
        release_child.set()
        try:
            await asyncio.wait_for(operation, timeout=1.0)
        except asyncio.CancelledError:
            pass
        await asyncio.wait_for(child, timeout=1.0)


@pytest.mark.asyncio
async def test_cancelled_disconnect_invalidates_inflight_connect(monkeypatch):
    """Shutdown intent survives cancellation while waiting for connect's lock."""
    import plugins.platforms.feishu.adapter as feishu_module

    adapter = FeishuAdapter(PlatformConfig())
    adapter._app_id = "cli_test"
    adapter._app_secret = "secret_test"
    monkeypatch.setattr(feishu_module, "_load_lark_oapi", lambda: True)
    monkeypatch.setattr(
        feishu_module,
        "acquire_scoped_lock",
        lambda *_args, **_kwargs: (True, None),
    )
    monkeypatch.setattr(feishu_module, "release_scoped_lock", lambda *_args: None)
    monkeypatch.setattr(adapter, "_persist_seen_message_ids", lambda: None)
    monkeypatch.setattr(adapter, "_mark_disconnected", lambda: None)
    monkeypatch.setattr(adapter, "_set_fatal_error", lambda *_args, **_kwargs: None)

    async def direct_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(feishu_module.asyncio, "to_thread", direct_to_thread)
    connect_paused = asyncio.Event()
    release_connect = asyncio.Event()

    async def paused_connect_with_retry() -> None:
        connect_paused.set()
        await release_connect.wait()

    monkeypatch.setattr(adapter, "_connect_with_retry", paused_connect_with_retry)
    connect_task = asyncio.create_task(adapter.connect())
    await asyncio.wait_for(connect_paused.wait(), timeout=1.0)
    disconnect_task = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    assert adapter._ws_disconnect_count == 1
    disconnect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect_task
    assert adapter._ws_disconnect_count == 0

    release_connect.set()
    connected = await asyncio.wait_for(connect_task, timeout=1.0)

    assert connected is False
    assert adapter._ws_runtime is None
    assert adapter._ws_disconnect_count == 0
