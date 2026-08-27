"""Regression tests for Matrix sync-task lifecycle and health propagation."""

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.matrix.adapter import MatrixAdapter
from tests.gateway.test_matrix import _make_fake_mautrix


def _fresh_adapter() -> MatrixAdapter:
    adapter = MatrixAdapter(
        PlatformConfig(
            enabled=True,
            token="syt_test_token",
            extra={
                "homeserver": "https://matrix.example.org",
                "user_id": "@hermes:example.org",
            },
        )
    )
    adapter._e2ee_mode = "off"
    adapter._encryption = False
    return adapter


def _make_adapter() -> MatrixAdapter:
    adapter = _fresh_adapter()
    adapter._client = MagicMock()
    adapter._client.sync_store = MagicMock()
    adapter._client.sync_store.get_next_batch = AsyncMock(return_value=None)
    adapter._joined_rooms = set()
    adapter._closing = False
    adapter._lifecycle_generation = 1
    adapter._active_generation = 1
    adapter._mark_connected()
    return adapter


def _publish_task(
    adapter: MatrixAdapter, task: asyncio.Task, generation: int = 1
) -> None:
    adapter._lifecycle_generation = max(adapter._lifecycle_generation, generation)
    adapter._active_generation = generation
    adapter._sync_task = task
    adapter._published_sync = (generation, task)


def _connect_client() -> MagicMock:
    client = MagicMock()
    client.mxid = "@hermes:example.org"
    client.device_id = None
    client.crypto = None
    client.whoami = AsyncMock(
        return_value=MagicMock(
            user_id="@hermes:example.org",
            device_id="DEVICE",
        )
    )
    client.sync_store = MagicMock()
    client.sync_store.get_next_batch = AsyncMock(return_value=None)
    client.sync_store.put_next_batch = AsyncMock()
    client.add_dispatcher = MagicMock()
    client.add_event_handler = MagicMock()
    client.handle_sync = MagicMock(return_value=[])
    return client


def _connect_modules(*clients: MagicMock) -> dict:
    modules = _make_fake_mautrix()
    remaining = iter(clients)

    class HTTPAPI:
        def __init__(self, base_url="", token="", client_session=None, **_kwargs):
            self.base_url = base_url
            self.token = token
            self.session = client_session

    def build_client(**kwargs):
        client = next(remaining)
        client.api = kwargs["api"]
        return client

    modules["mautrix.api"].HTTPAPI = HTTPAPI
    modules["mautrix.client"].Client = MagicMock(side_effect=build_client)
    return modules

@pytest.mark.asyncio
async def test_unexpected_consumer_exit_is_generically_retryable():
    class SyncError:
        message = "M_UNKNOWN_TOKEN: expired"

    adapter = _make_adapter()
    adapter._client.sync = AsyncMock(return_value=SyncError())
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)

    task = asyncio.create_task(adapter._sync_loop())
    _publish_task(adapter, task)
    task.add_done_callback(
        lambda completed: adapter._handle_sync_task_done(completed, 1)
    )
    await task
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert adapter.is_connected is False
    assert adapter.fatal_error_code == "matrix_sync_task_exited"
    assert adapter.fatal_error_retryable is True
    fatal_handler.assert_awaited_once_with(adapter)


@pytest.mark.asyncio
async def test_existing_terminal_reason_survives_consumer_exit():
    adapter = _make_adapter()
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    task = asyncio.create_task(asyncio.sleep(0))
    _publish_task(adapter, task)
    adapter._set_fatal_error("future_classifier", "classified elsewhere", retryable=False)
    await task

    adapter._handle_sync_task_done(task, 1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert adapter.fatal_error_code == "future_classifier"
    assert adapter.fatal_error_retryable is False
    fatal_handler.assert_awaited_once_with(adapter)


@pytest.mark.asyncio
async def test_startup_failure_returns_to_owner_without_supervisor_notification():
    adapter = _make_adapter()
    adapter._running = False
    adapter._client.sync_store.get_next_batch = AsyncMock(
        side_effect=RuntimeError("sync store unavailable")
    )
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    started = asyncio.get_running_loop().create_future()
    published = asyncio.Event()

    await adapter._sync_loop(started, published)

    assert started.result() is False
    assert adapter.has_fatal_error is False
    fatal_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_consumer_cannot_poll_before_connect_publishes_state():
    adapter = _make_adapter()
    adapter._running = False
    polled = asyncio.Event()

    async def sync(**_kwargs):
        assert adapter.is_connected is True
        polled.set()
        raise asyncio.CancelledError

    adapter._client.sync = sync
    started = asyncio.get_running_loop().create_future()
    published = asyncio.Event()

    task = asyncio.create_task(adapter._sync_loop(started, published))
    assert await started is True
    await asyncio.sleep(0)
    assert polled.is_set() is False
    adapter._mark_connected()
    published.set()
    await task

    assert polled.is_set() is True


@pytest.mark.asyncio
async def test_connect_cancellation_during_consumer_start_closes_exact_generation():
    adapter = _fresh_adapter()
    client = _connect_client()
    client.sync = AsyncMock(return_value={"rooms": {"join": {}}})
    get_started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_get_next_batch():
        get_started.set()
        await never.wait()

    client.sync_store.get_next_batch = AsyncMock(
        side_effect=blocked_get_next_batch
    )
    session = MagicMock(close=AsyncMock())
    modules = _connect_modules(client)
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod, "_create_matrix_session", return_value=session
    ), patch.object(adapter, "_refresh_dm_cache", AsyncMock()):
        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(get_started.wait(), timeout=1)
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connect_task, timeout=1)

    assert adapter._active_generation is None
    assert adapter._published_sync is None
    assert adapter._sync_task is None
    assert adapter._client is None
    assert adapter.is_connected is False
    assert adapter.has_fatal_error is False
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_waits_for_connect_generation_then_tears_it_down():
    adapter = _fresh_adapter()
    client = _connect_client()
    initial_started = asyncio.Event()
    release_initial = asyncio.Event()
    consumer_never = asyncio.Event()
    calls = 0

    async def staged_sync(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            initial_started.set()
            await release_initial.wait()
            return {"rooms": {"join": {}}}
        await consumer_never.wait()

    client.sync = AsyncMock(side_effect=staged_sync)
    session = MagicMock(close=AsyncMock())
    modules = _connect_modules(client)
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod, "_create_matrix_session", return_value=session
    ), patch.object(adapter, "_refresh_dm_cache", AsyncMock()):
        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(initial_started.wait(), timeout=1)
        disconnect_task = asyncio.create_task(adapter.disconnect())
        await asyncio.sleep(0)
        assert disconnect_task.done() is False
        release_initial.set()
        assert await asyncio.wait_for(connect_task, timeout=1) is True
        await asyncio.wait_for(disconnect_task, timeout=1)

    assert adapter.is_connected is False
    assert adapter._active_generation is None
    assert adapter._sync_task is None
    assert adapter._client is None
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_old_disconnect_cannot_close_replacement_generation():
    adapter = _fresh_adapter()
    client_one = _connect_client()
    client_two = _connect_client()
    first_never = asyncio.Event()
    second_never = asyncio.Event()
    first_calls = 0
    second_calls = 0

    async def first_sync(**_kwargs):
        nonlocal first_calls
        first_calls += 1
        if first_calls == 1:
            return {"rooms": {"join": {}}}
        await first_never.wait()

    async def second_sync(**_kwargs):
        nonlocal second_calls
        second_calls += 1
        if second_calls == 1:
            return {"rooms": {"join": {}}}
        await second_never.wait()

    client_one.sync = AsyncMock(side_effect=first_sync)
    client_two.sync = AsyncMock(side_effect=second_sync)
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocked_old_close():
        close_started.set()
        await release_close.wait()

    session_one = MagicMock(close=AsyncMock(side_effect=blocked_old_close))
    session_two = MagicMock(close=AsyncMock())
    modules = _connect_modules(client_one, client_two)
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod,
        "_create_matrix_session",
        side_effect=[session_one, session_two],
    ), patch.object(adapter, "_refresh_dm_cache", AsyncMock()):
        assert await asyncio.wait_for(adapter.connect(), timeout=1) is True
        disconnect_task = asyncio.create_task(adapter.disconnect())
        await asyncio.wait_for(close_started.wait(), timeout=1)
        reconnect_task = asyncio.create_task(adapter.connect(is_reconnect=True))
        await asyncio.sleep(0)
        assert reconnect_task.done() is False
        assert modules["mautrix.client"].Client.call_count == 1

        release_close.set()
        await asyncio.wait_for(disconnect_task, timeout=1)
        assert await asyncio.wait_for(reconnect_task, timeout=1) is True
        assert adapter._client is client_two
        assert adapter.is_connected is True
        session_one.close.assert_awaited_once()
        session_two.close.assert_not_awaited()

        await asyncio.wait_for(adapter.disconnect(), timeout=1)

    session_two.close.assert_awaited_once()
    assert adapter._client is None
    assert adapter.is_connected is False


@pytest.mark.asyncio
async def test_reconnect_waits_for_admitted_fatal_handler_to_finish():
    adapter = _make_adapter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handler(_adapter):
        entered.set()
        await release.wait()

    adapter.set_fatal_error_handler(blocked_handler)
    old_task = asyncio.create_task(asyncio.sleep(0))
    _publish_task(adapter, old_task, generation=1)
    await old_task
    adapter._handle_sync_task_done(old_task, 1)
    await asyncio.wait_for(entered.wait(), timeout=1)

    replacement_client = _connect_client()
    replacement_never = asyncio.Event()
    replacement_calls = 0

    async def replacement_sync(**_kwargs):
        nonlocal replacement_calls
        replacement_calls += 1
        if replacement_calls == 1:
            return {"rooms": {"join": {}}}
        await replacement_never.wait()

    replacement_client.sync = AsyncMock(side_effect=replacement_sync)
    replacement_session = MagicMock(close=AsyncMock())
    modules = _connect_modules(replacement_client)
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod,
        "_create_matrix_session",
        return_value=replacement_session,
    ), patch.object(adapter, "_refresh_dm_cache", AsyncMock()):
        reconnect_task = asyncio.create_task(adapter.connect(is_reconnect=True))
        await asyncio.sleep(0)
        assert reconnect_task.done() is False
        assert adapter._active_generation == 1

        release.set()
        assert await asyncio.wait_for(reconnect_task, timeout=1) is True
        assert adapter._active_generation == 2
        assert adapter._client is replacement_client
        assert adapter.is_connected is True
        await asyncio.wait_for(adapter.disconnect(), timeout=1)

    replacement_session.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_resource", ["crypto", "session"])
async def test_cancelled_disconnect_retains_cleanup_ownership_until_done(
    blocked_resource
):
    adapter = _make_adapter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_close():
        entered.set()
        await release.wait()

    crypto_stop = AsyncMock(
        side_effect=blocked_close if blocked_resource == "crypto" else None
    )
    session_close = AsyncMock(
        side_effect=blocked_close if blocked_resource == "session" else None
    )
    adapter._crypto_db = MagicMock(stop=crypto_stop)
    adapter._client.api.session.close = session_close

    disconnect_task = asyncio.create_task(adapter.disconnect())
    await asyncio.wait_for(entered.wait(), timeout=1)
    cleanup_task = adapter._lifecycle_cleanup_task
    assert cleanup_task is not None
    disconnect_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect_task

    assert cleanup_task.done() is False
    assert adapter._client is None
    assert adapter._crypto_db is None
    retry_disconnect = asyncio.create_task(adapter.disconnect())
    await asyncio.sleep(0)
    assert retry_disconnect.done() is False

    release.set()
    await asyncio.wait_for(cleanup_task, timeout=1)
    await asyncio.wait_for(retry_disconnect, timeout=1)

    crypto_stop.assert_awaited_once()
    session_close.assert_awaited_once()
    assert adapter._lifecycle_cleanup_task is None
    assert adapter.is_connected is False


@pytest.mark.asyncio
async def test_client_constructor_failure_closes_unpublished_http_session():
    adapter = _fresh_adapter()
    session = MagicMock(close=AsyncMock())
    modules = _make_fake_mautrix()
    modules["mautrix.client"].Client = MagicMock(
        side_effect=RuntimeError("client construction failed")
    )
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod, "_create_matrix_session", return_value=session
    ):
        with pytest.raises(RuntimeError, match="client construction failed"):
            await adapter.connect()

    session.close.assert_awaited_once()
    assert adapter._active_generation is None
    assert adapter._client is None
    assert adapter._pending_session is None
    assert adapter._lifecycle_cleanup_task is None


@pytest.mark.asyncio
async def test_cancel_during_constructor_failure_close_keeps_session_owned():
    adapter = _fresh_adapter()
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    async def blocked_close():
        close_started.set()
        await release_close.wait()

    session = MagicMock(close=AsyncMock(side_effect=blocked_close))
    modules = _make_fake_mautrix()
    modules["mautrix.client"].Client = MagicMock(
        side_effect=RuntimeError("client construction failed")
    )
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod, "_create_matrix_session", return_value=session
    ):
        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(close_started.wait(), timeout=1)
        cleanup_task = adapter._lifecycle_cleanup_task
        assert cleanup_task is not None
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect_task

        assert cleanup_task.done() is False
        assert adapter._pending_session is None
        release_close.set()
        await asyncio.wait_for(cleanup_task, timeout=1)

    session.close.assert_awaited_once()
    assert adapter._lifecycle_cleanup_task is None
    assert adapter._active_generation is None


@pytest.mark.asyncio
async def test_cancel_during_crypto_database_start_still_stops_database():
    adapter = _fresh_adapter()
    adapter._e2ee_mode = "required"
    adapter._encryption = True
    client = _connect_client()
    session = MagicMock(close=AsyncMock())
    start_entered = asyncio.Event()
    never = asyncio.Event()

    async def blocked_start():
        start_entered.set()
        await never.wait()

    crypto_db = MagicMock(
        start=AsyncMock(side_effect=blocked_start),
        stop=AsyncMock(),
    )
    modules = _connect_modules(client)
    modules["mautrix.util.async_db"].Database.create = MagicMock(
        return_value=crypto_db
    )
    import plugins.platforms.matrix.adapter as matrix_mod

    with patch.dict("sys.modules", modules), patch.object(
        matrix_mod, "_create_matrix_session", return_value=session
    ), patch.object(matrix_mod, "_check_e2ee_deps", return_value=True):
        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(start_entered.wait(), timeout=1)
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect_task

    crypto_db.stop.assert_awaited_once()
    session.close.assert_awaited_once()
    assert adapter._crypto_db is None
    assert adapter._client is None
    assert adapter._active_generation is None
    assert adapter._lifecycle_cleanup_task is None


@pytest.mark.asyncio
async def test_notification_carrier_is_retained_while_handler_is_blocked_and_drains():
    adapter = _make_adapter()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_handler(_adapter):
        entered.set()
        await release.wait()

    adapter.set_fatal_error_handler(blocked_handler)
    task = asyncio.create_task(asyncio.sleep(0))
    _publish_task(adapter, task)
    await task
    adapter._handle_sync_task_done(task, 1)
    await entered.wait()

    assert len(adapter._sync_notification_tasks) == 1
    gc.collect()
    assert len(adapter._sync_notification_tasks) == 1

    carriers = tuple(adapter._sync_notification_tasks)
    release.set()
    await asyncio.gather(*carriers)
    await asyncio.sleep(0)
    assert adapter._sync_notification_tasks == set()


@pytest.mark.asyncio
async def test_planned_disconnect_task_completion_is_not_fatal():
    adapter = _make_adapter()
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    task = asyncio.create_task(asyncio.sleep(0))
    _publish_task(adapter, task)
    # _disconnect_locked clears publication before a planned cancellation.
    adapter._closing = True
    adapter._published_sync = None
    await task

    adapter._handle_sync_task_done(task, 1)
    await asyncio.sleep(0)

    assert adapter.has_fatal_error is False
    fatal_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent_and_closes_resources_once():
    adapter = _make_adapter()
    session_close = AsyncMock()
    adapter._client.api.session.close = session_close
    crypto_stop = AsyncMock()
    adapter._crypto_db = MagicMock(stop=crypto_stop)
    task = asyncio.create_task(asyncio.Event().wait())
    _publish_task(adapter, task)
    task.add_done_callback(
        lambda completed: adapter._handle_sync_task_done(completed, 1)
    )

    await adapter.disconnect()
    await adapter.disconnect()

    assert adapter.is_connected is False
    assert adapter._sync_task is None
    session_close.assert_awaited_once()
    crypto_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_completed_task_cannot_poison_none_transition():
    adapter = _make_adapter()
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    old_task = asyncio.create_task(asyncio.sleep(0))
    await old_task
    adapter._sync_task = None

    adapter._handle_sync_task_done(old_task, 1)
    await asyncio.sleep(0)

    assert adapter.is_connected is True
    assert adapter.has_fatal_error is False
    fatal_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_completed_task_cannot_fail_replacement_consumer():
    adapter = _make_adapter()
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    old_task = asyncio.create_task(asyncio.sleep(0))
    await old_task
    replacement = asyncio.create_task(asyncio.Event().wait())
    _publish_task(adapter, replacement, generation=2)

    adapter._handle_sync_task_done(old_task, 1)
    await asyncio.sleep(0)

    assert adapter.is_connected is True
    assert adapter.has_fatal_error is False
    fatal_handler.assert_not_awaited()
    replacement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement


@pytest.mark.asyncio
async def test_stale_notification_carrier_cannot_fail_replacement_generation():
    adapter = _make_adapter()
    fatal_handler = AsyncMock()
    adapter.set_fatal_error_handler(fatal_handler)
    old_task = asyncio.create_task(asyncio.sleep(0))
    _publish_task(adapter, old_task, generation=1)
    await old_task

    # Scheduling the carrier does not run it until this coroutine yields.
    adapter._handle_sync_task_done(old_task, 1)
    replacement = asyncio.create_task(asyncio.Event().wait())
    _publish_task(adapter, replacement, generation=2)
    adapter._mark_connected()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    fatal_handler.assert_not_awaited()
    assert adapter.is_connected is True
    assert adapter.has_fatal_error is False
    replacement.cancel()
    with pytest.raises(asyncio.CancelledError):
        await replacement
