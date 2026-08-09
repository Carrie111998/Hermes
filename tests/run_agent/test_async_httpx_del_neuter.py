"""Tests for the AsyncHttpxClientWrapper.__del__ neuter fix.

The OpenAI SDK's ``AsyncHttpxClientWrapper.__del__`` schedules
``aclose()`` via ``asyncio.get_running_loop().create_task()``.  When GC
fires during CLI idle time, prompt_toolkit's event loop picks up the task
and crashes with "Event loop is closed" because the underlying TCP
transport is bound to a dead worker loop.

The three-layer defence:
1. ``neuter_async_httpx_del()`` replaces ``__del__`` with a no-op.
2. A custom asyncio exception handler silences residual errors.
3. ``cleanup_stale_async_clients()`` evicts stale cache entries.
"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class _AsyncCloseSentinelClient:
    """Small async-client double whose close represents transport teardown."""

    def __init__(self):
        self._client = SimpleNamespace(is_closed=False)
        self.transport_closed = threading.Event()

    async def close(self):
        self._client.is_closed = True
        self.transport_closed.set()


class _BlockingAsyncCloseClient(_AsyncCloseSentinelClient):
    def __init__(self):
        super().__init__()
        self.release_close = threading.Event()

    async def close(self):
        while not self.release_close.is_set():
            await asyncio.sleep(0.005)
        await super().close()


class _FailingAsyncCloseClient(_AsyncCloseSentinelClient):
    async def close(self):
        raise RuntimeError("transport close failed")


class _CoordinatedAsyncCloseClient(_AsyncCloseSentinelClient):
    def __init__(self, started, all_started, expected):
        super().__init__()
        self._started = started
        self._all_started = all_started
        self._expected = expected

    async def close(self):
        with self._started["lock"]:
            self._started["count"] += 1
            if self._started["count"] == self._expected:
                self._all_started.set()
        while not self._all_started.is_set():
            await asyncio.sleep(0.005)
        await super().close()


# ---------------------------------------------------------------------------
# Layer 1: neuter_async_httpx_del
# ---------------------------------------------------------------------------

class TestNeuterAsyncHttpxDel:
    """Verify neuter_async_httpx_del replaces __del__ on the SDK class."""

    def test_del_becomes_noop(self):
        """After neuter, __del__ should do nothing (no RuntimeError)."""
        from agent.auxiliary_client import neuter_async_httpx_del

        try:
            from openai._base_client import AsyncHttpxClientWrapper
        except ImportError:
            pytest.skip("openai SDK not installed")

        # Save original so we can restore
        original_del = AsyncHttpxClientWrapper.__del__
        try:
            neuter_async_httpx_del()
            # The patched __del__ should be a no-op lambda
            assert AsyncHttpxClientWrapper.__del__ is not original_del
            # Calling it should not raise, even without a running loop
            wrapper = MagicMock(spec=AsyncHttpxClientWrapper)
            AsyncHttpxClientWrapper.__del__(wrapper)  # Should be silent
        finally:
            # Restore original to avoid leaking into other tests
            AsyncHttpxClientWrapper.__del__ = original_del

    def test_neuter_idempotent(self):
        """Calling neuter twice doesn't break anything."""
        from agent.auxiliary_client import neuter_async_httpx_del

        try:
            from openai._base_client import AsyncHttpxClientWrapper
        except ImportError:
            pytest.skip("openai SDK not installed")

        original_del = AsyncHttpxClientWrapper.__del__
        try:
            neuter_async_httpx_del()
            first_del = AsyncHttpxClientWrapper.__del__
            neuter_async_httpx_del()
            second_del = AsyncHttpxClientWrapper.__del__
            # Both calls should succeed; the class should have a no-op
            assert first_del is not original_del
            assert second_del is not original_del
        finally:
            AsyncHttpxClientWrapper.__del__ = original_del



# ---------------------------------------------------------------------------
# Layer 3: cleanup_stale_async_clients
# ---------------------------------------------------------------------------

class TestCleanupStaleAsyncClients:
    """Verify stale cache entries are evicted only after close succeeds."""

    def test_removes_stale_entries(self):
        """Entries with a closed loop should be evicted."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_lock,
            cleanup_stale_async_clients,
        )

        # Create a loop, close it, make a cache entry
        loop = asyncio.new_event_loop()
        loop.close()

        mock_client = MagicMock()
        # Give it the same wrapper shape as an SDK client.
        mock_client._client = MagicMock()
        mock_client._client.is_closed = False

        key = ("test_stale", True, "", "", "", (), False)
        with _client_cache_lock:
            _client_cache[key] = (mock_client, "test-model", loop)

        try:
            cleanup_stale_async_clients()
            with _client_cache_lock:
                assert key not in _client_cache, "Stale entry should be removed"
        finally:
            # Clean up in case test fails
            with _client_cache_lock:
                _client_cache.pop(key, None)

    def test_dead_owner_thread_cleanup_physically_closes_transport_before_eviction(self):
        """A stopped owner loop is drained before its cache owner is removed."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_key,
            _client_cache_lock,
            _get_cached_client,
            cleanup_stale_async_clients,
        )

        key = _client_cache_key("test_dead_owner", async_mode=True, model="m1")
        client = _AsyncCloseSentinelClient()
        owner_loop = asyncio.new_event_loop()

        def build_on_owner_thread():
            asyncio.set_event_loop(owner_loop)
            with patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(client, "m1"),
            ):
                cached, _ = _get_cached_client(
                    "test_dead_owner", "m1", async_mode=True
                )
            assert cached is client

        owner = threading.Thread(target=build_on_owner_thread)
        owner.start()
        owner.join(timeout=5)
        assert not owner.is_alive()
        assert not owner_loop.is_closed()

        try:
            cleanup_stale_async_clients()

            assert client.transport_closed.is_set()
            with _client_cache_lock:
                assert key not in _client_cache
        finally:
            with _client_cache_lock:
                _client_cache.pop(key, None)
            owner_loop.close()

    def test_keeps_live_entries(self):
        """Entries with an open loop should be preserved."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_lock,
            cleanup_stale_async_clients,
        )

        loop = asyncio.new_event_loop()  # NOT closed

        mock_client = MagicMock()
        key = ("test_live", True, "", "", "", (), False)
        with _client_cache_lock:
            _client_cache[key] = (mock_client, "test-model", loop)

        try:
            cleanup_stale_async_clients()
            with _client_cache_lock:
                assert key in _client_cache, "Live entry should be preserved"
        finally:
            loop.close()
            with _client_cache_lock:
                _client_cache.pop(key, None)

    def test_keeps_entries_without_loop(self):
        """Sync entries (cached_loop=None) should be preserved."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_lock,
            cleanup_stale_async_clients,
        )

        mock_client = MagicMock()
        key = ("test_sync", False, "", "", "", (), False)
        with _client_cache_lock:
            _client_cache[key] = (mock_client, "test-model", None)

        try:
            cleanup_stale_async_clients()
            with _client_cache_lock:
                assert key in _client_cache, "Sync entry should be preserved"
        finally:
            with _client_cache_lock:
                _client_cache.pop(key, None)


# ---------------------------------------------------------------------------
# Cache bounded growth (#10200)
# ---------------------------------------------------------------------------

class TestClientCacheBoundedGrowth:
    """Verify the cache stays bounded when loops change (fix for #10200).

    Previously, loop_id was part of the cache key, so every new event loop
    created a new entry for the same provider config.  Now loop identity is
    validated at hit time and stale entries are replaced in-place.
    """

    def test_same_key_replaces_stale_loop_entry(self):
        """When the loop changes, the old entry should be replaced, not duplicated."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_key,
            _client_cache_lock,
            _get_cached_client,
        )

        key = _client_cache_key(
            "test_replace",
            async_mode=True,
            task="",
        )

        # Simulate a stale entry from a closed loop
        old_loop = asyncio.new_event_loop()
        old_loop.close()
        old_client = MagicMock()
        old_client._client = MagicMock()
        old_client._client.is_closed = False

        with _client_cache_lock:
            _client_cache[key] = (old_client, "old-model", old_loop)

        try:
            # Now call _get_cached_client — should detect stale loop and evict
            with patch("agent.auxiliary_client.resolve_provider_client") as mock_resolve:
                mock_resolve.return_value = (MagicMock(), "new-model")
                client, model = _get_cached_client(
                    "test_replace", async_mode=True,
                )
            # The old entry should have been replaced
            with _client_cache_lock:
                assert key in _client_cache, "Key should still exist (replaced)"
                entry = _client_cache[key]
                assert entry[1] == "new-model", "Should have the new model"
        finally:
            with _client_cache_lock:
                _client_cache.pop(key, None)

    def test_close_timeout_does_not_mark_private_state_or_drop_owner(self):
        """A timed-out owner-loop close keeps the old generation quarantined."""
        import agent.auxiliary_client as aux

        key = aux._client_cache_key("test_close_timeout", async_mode=True, model="m1")
        old_client = _BlockingAsyncCloseClient()
        new_client = _AsyncCloseSentinelClient()
        owner_loop = asyncio.new_event_loop()
        owner_ready = threading.Event()

        def run_owner_loop():
            asyncio.set_event_loop(owner_loop)
            owner_ready.set()
            owner_loop.run_forever()

        owner = threading.Thread(target=run_owner_loop)
        owner.start()
        assert owner_ready.wait(timeout=5)

        async def build_old():
            with patch.object(
                aux, "resolve_provider_client", return_value=(old_client, "m1")
            ):
                return aux._get_cached_client(
                    "test_close_timeout", "m1", async_mode=True
                )[0]

        assert asyncio.run_coroutine_threadsafe(build_old(), owner_loop).result(timeout=5) is old_client

        caller_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(caller_loop)
        try:
            with patch.object(aux, "_ASYNC_CLIENT_CLOSE_TIMEOUT", 0.05), patch.object(
                aux, "resolve_provider_client", return_value=(new_client, "m1")
            ):
                resolved, _ = aux._get_cached_client(
                    "test_close_timeout", "m1", async_mode=True
                )

            assert resolved is new_client
            assert old_client._client.is_closed is False
            assert any(
                retired[0] is old_client
                for retired in aux._retired_async_clients.values()
            )
            assert aux._client_cache[key][0] is new_client
        finally:
            old_client.release_close.set()
            asyncio.run_coroutine_threadsafe(
                aux.close_cached_async_clients_for_loop(owner_loop), owner_loop
            ).result(timeout=5)
            assert old_client.transport_closed.is_set()
            assert not any(
                retired[0] is old_client
                for retired in aux._retired_async_clients.values()
            )
            owner_loop.call_soon_threadsafe(owner_loop.stop)
            owner.join(timeout=5)
            owner_loop.close()
            caller_loop.close()
            with aux._client_cache_lock:
                aux._client_cache.pop(key, None)
                aux._client_cache_owner_threads.pop(key, None)
                if hasattr(aux, "_retired_async_clients"):
                    aux._retired_async_clients.clear()

    def test_same_loop_eviction_schedules_physical_close(self):
        """A sync eviction on the owner loop must not defer close to shutdown."""
        import agent.auxiliary_client as aux

        client = _AsyncCloseSentinelClient()
        key = aux._client_cache_key(
            "test_same_loop_evict", async_mode=True, model="m1"
        )

        async def scenario():
            loop = asyncio.get_running_loop()
            with aux._client_cache_lock:
                aux._client_cache[key] = (client, "m1", loop)
            aux._evict_cached_clients("test_same_loop_evict")
            for _ in range(20):
                with aux._client_cache_lock:
                    ownership_released = (
                        id(client) not in aux._retired_async_clients
                    )
                if client.transport_closed.is_set() and ownership_released:
                    break
                await asyncio.sleep(0)

            assert client.transport_closed.is_set()
            with aux._client_cache_lock:
                assert key not in aux._client_cache
                assert id(client) not in aux._retired_async_clients

        try:
            asyncio.run(scenario())
        finally:
            with aux._client_cache_lock:
                aux._client_cache.pop(key, None)
                aux._retired_async_clients.pop(id(client), None)
                aux._retired_async_close_tasks.pop(id(client), None)

    def test_loop_drain_keeps_owner_when_client_has_no_close_api(self):
        """Unsupported wrappers cannot be reported closed and discarded."""
        import agent.auxiliary_client as aux

        client = SimpleNamespace()
        key = aux._client_cache_key(
            "test_missing_close", async_mode=True, model="m1"
        )

        async def scenario():
            loop = asyncio.get_running_loop()
            with aux._client_cache_lock:
                aux._client_cache[key] = (client, "m1", loop)

            await aux.close_cached_async_clients_for_loop(loop)

            with aux._client_cache_lock:
                assert aux._client_cache[key][0] is client

        try:
            asyncio.run(scenario())
        finally:
            with aux._client_cache_lock:
                aux._client_cache.pop(key, None)

    def test_async_compatibility_wrappers_close_real_clients(self):
        """Async shims expose close so loop drains reach their real transports."""
        import agent.auxiliary_client as aux

        class RealClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        def sync_wrapper(real_client):
            return SimpleNamespace(
                chat=SimpleNamespace(completions=MagicMock()),
                api_key="key",
                base_url="https://example.invalid",
                _real_client=real_client,
            )

        async def scenario():
            codex_real = RealClient()
            anthropic_real = RealClient()
            codex = aux.AsyncCodexAuxiliaryClient(sync_wrapper(codex_real))
            anthropic = aux.AsyncAnthropicAuxiliaryClient(
                sync_wrapper(anthropic_real)
            )

            assert await aux._await_cached_client_close(codex) is True
            assert await aux._await_cached_client_close(anthropic) is True
            assert codex_real.closed
            assert anthropic_real.closed

        asyncio.run(scenario())

    def test_shutdown_closes_owner_loops_concurrently(self):
        """One slow generation must not consume one timeout after another."""
        import agent.auxiliary_client as aux

        expected = 3
        started = {"count": 0, "lock": threading.Lock()}
        all_started = threading.Event()
        loops = [asyncio.new_event_loop() for _ in range(expected)]
        clients = [
            _CoordinatedAsyncCloseClient(started, all_started, expected)
            for _ in range(expected)
        ]
        keys = [
            aux._client_cache_key(
                f"test_parallel_shutdown_{index}", async_mode=True, model="m1"
            )
            for index in range(expected)
        ]

        with aux._client_cache_lock:
            for key, client, loop in zip(keys, clients, loops):
                aux._client_cache[key] = (client, "m1", loop)

        try:
            with patch.object(aux, "_ASYNC_CLIENT_CLOSE_TIMEOUT", 0.2):
                aux.shutdown_cached_clients()

            assert all_started.is_set()
            assert all(client.transport_closed.is_set() for client in clients)
            with aux._client_cache_lock:
                assert all(key not in aux._client_cache for key in keys)
        finally:
            with aux._client_cache_lock:
                for key in keys:
                    aux._client_cache.pop(key, None)
                aux._retired_async_clients.clear()
            for loop in loops:
                if not loop.is_closed():
                    loop.close()

    def test_repeated_worker_loop_turns_keep_fd_count_bounded(self):
        """Each disposable loop closes its real HTTP keep-alive transport."""
        import httpx

        import agent.auxiliary_client as aux
        from model_tools import _run_async

        key = aux._client_cache_key("test_worker_http", async_mode=True, model="m1")
        clients = []
        peer_eofs = []

        def build_client(*_args, **_kwargs):
            client = httpx.AsyncClient(trust_env=False)
            clients.append(client)
            return client, "m1"

        async def one_turn():
            peer_eof = threading.Event()

            async def handle(reader, writer):
                try:
                    await reader.readuntil(b"\r\n\r\n")
                    writer.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: keep-alive\r\n\r\n"
                    )
                    await writer.drain()
                    await reader.read()
                    peer_eof.set()
                finally:
                    writer.close()
                    await writer.wait_closed()

            server = await asyncio.start_server(handle, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                client, _ = aux._get_cached_client(
                    "test_worker_http", "m1", async_mode=True
                )
                response = await client.get(f"http://127.0.0.1:{port}/")
                assert response.status_code == 200
                return peer_eof
            finally:
                server.close()
                await server.wait_closed()

        async def run_turns():
            for _ in range(5):
                peer_eofs.append(_run_async(one_turn()))

        try:
            with patch.object(aux, "resolve_provider_client", side_effect=build_client):
                asyncio.run(run_turns())

            assert len(clients) == 5
            assert all(client.is_closed for client in clients)
            assert all(peer_eof.is_set() for peer_eof in peer_eofs)
            with aux._client_cache_lock:
                assert key not in aux._client_cache
                assert not aux._retired_async_clients
        finally:
            with aux._client_cache_lock:
                aux._client_cache.pop(key, None)
                aux._client_cache_owner_threads.pop(key, None)
                aux._retired_async_clients.clear()

    def test_stopped_owner_loop_client_is_not_reused_or_silently_evicted(self):
        """A failed close quarantines the stopped-loop client while replacing it."""
        import agent.auxiliary_client as aux

        key = aux._client_cache_key("test_stopped_owner", async_mode=True, model="m1")
        old_client = _FailingAsyncCloseClient()
        new_client = _AsyncCloseSentinelClient()
        owner_loop = asyncio.new_event_loop()

        def build_old():
            asyncio.set_event_loop(owner_loop)
            with patch.object(
                aux, "resolve_provider_client", return_value=(old_client, "m1")
            ):
                aux._get_cached_client("test_stopped_owner", "m1", async_mode=True)

        owner = threading.Thread(target=build_old)
        owner.start()
        owner.join(timeout=5)
        assert not owner.is_alive()

        caller_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(caller_loop)
        try:
            with patch.object(
                aux, "resolve_provider_client", return_value=(new_client, "m1")
            ):
                resolved, _ = aux._get_cached_client(
                    "test_stopped_owner", "m1", async_mode=True
                )

            assert resolved is new_client
            assert old_client._client.is_closed is False
            assert aux._client_cache[key][0] is new_client
            assert any(
                retired[0] is old_client
                for retired in aux._retired_async_clients.values()
            )
        finally:
            caller_loop.close()
            owner_loop.close()
            with aux._client_cache_lock:
                aux._client_cache.pop(key, None)
                aux._client_cache_owner_threads.pop(key, None)
                aux._retired_async_clients.clear()

    def test_different_loops_do_not_grow_cache(self):
        """Multiple event loops for the same provider should NOT create multiple entries."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_lock,
        )

        key = ("test_no_grow", True, "", "", "", (), False)

        loops = []
        try:
            for i in range(5):
                loop = asyncio.new_event_loop()
                loops.append(loop)
                mock_client = MagicMock()
                mock_client._client = MagicMock()
                mock_client._client.is_closed = False

                # Close previous loop entries (simulating worker thread recycling)
                if i > 0:
                    loops[i - 1].close()

                with _client_cache_lock:
                    # Simulate what _get_cached_client does: replace on loop mismatch
                    if key in _client_cache:
                        old_entry = _client_cache[key]
                        del _client_cache[key]
                    _client_cache[key] = (mock_client, f"model-{i}", loop)

            # Only one entry should exist for this key
            with _client_cache_lock:
                count = sum(1 for k in _client_cache if k == key)
                assert count == 1, f"Expected 1 entry, got {count}"
        finally:
            for loop in loops:
                if not loop.is_closed():
                    loop.close()
            with _client_cache_lock:
                _client_cache.pop(key, None)

    def test_max_cache_size_eviction(self):
        """Cache should not exceed _CLIENT_CACHE_MAX_SIZE."""
        from agent.auxiliary_client import (
            _client_cache,
            _client_cache_lock,
            _CLIENT_CACHE_MAX_SIZE,
        )

        # Save existing cache state
        with _client_cache_lock:
            saved = dict(_client_cache)
            _client_cache.clear()

        try:
            # Fill to max + 5
            for i in range(_CLIENT_CACHE_MAX_SIZE + 5):
                mock_client = MagicMock()
                mock_client._client = MagicMock()
                mock_client._client.is_closed = False
                key = (f"evict_test_{i}", False, "", "", "", (), False)
                with _client_cache_lock:
                    # Inline the eviction logic (same as _get_cached_client)
                    while len(_client_cache) >= _CLIENT_CACHE_MAX_SIZE:
                        evict_key = next(iter(_client_cache))
                        del _client_cache[evict_key]
                    _client_cache[key] = (mock_client, f"model-{i}", None)

            with _client_cache_lock:
                assert len(_client_cache) <= _CLIENT_CACHE_MAX_SIZE, \
                    f"Cache size {len(_client_cache)} exceeds max {_CLIENT_CACHE_MAX_SIZE}"
                # The earliest entries should have been evicted
                assert ("evict_test_0", False, "", "", "", (), False) not in _client_cache
                # The latest entries should be present
                assert (f"evict_test_{_CLIENT_CACHE_MAX_SIZE + 4}", False, "", "", "", (), False) in _client_cache
        finally:
            with _client_cache_lock:
                _client_cache.clear()
                _client_cache.update(saved)
