"""Regression coverage for cross-process MCP OAuth refresh rotation.

OAuth providers such as Canva rotate refresh tokens on every successful
refresh. Two Hermes processes that load the same expired token must not both
send that single-use refresh token. The first process refreshes and persists
the replacement; the second must then reload the replacement from disk and
use the fresh access token without issuing another refresh request.
"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import queue as queue_module
import time
from pathlib import Path

import pytest


pytest.importorskip("mcp.client.auth.oauth2", reason="MCP SDK 2.0+ required")


def _classify_request(request) -> str:
    return "refresh" if str(request.url) == "https://auth.example.com/token" else "mcp"


def _refresh_worker(
    hermes_home: str,
    start_barrier,
    release_refresh,
    results,
) -> None:
    """Drive one provider through its first auth-flow request in a child process."""
    os.environ["HERMES_HOME"] = hermes_home

    async def _run() -> None:
        from mcp.shared.auth import OAuthClientMetadata
        from pydantic import AnyUrl

        from tools.mcp_oauth import HermesTokenStorage
        from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
        from tools.mcp_tool import sdk_httpx

        assert _HERMES_PROVIDER_CLS is not None
        httpx = sdk_httpx()
        storage = HermesTokenStorage("srv")

        async def _noop_redirect(_url: str) -> None:
            return None

        async def _noop_callback() -> tuple[str, str | None]:
            raise AssertionError("browser authorization must not run")

        provider = _HERMES_PROVIDER_CLS(
            server_name="srv",
            server_url="https://mcp.example.com/mcp",
            client_metadata=OAuthClientMetadata(
                redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
                client_name="Hermes Agent test",
            ),
            storage=storage,
            redirect_handler=_noop_redirect,
            callback_handler=_noop_callback,
        )

        await asyncio.to_thread(start_barrier.wait)
        flow = provider.async_auth_flow(
            httpx.Request("POST", "https://mcp.example.com/mcp")
        )
        first_request = await flow.__anext__()
        first_kind = _classify_request(first_request)
        results.put(
            ("first", os.getpid(), first_kind, time.monotonic(), release_refresh.is_set())
        )

        if first_kind == "refresh":
            released = await asyncio.to_thread(release_refresh.wait, 10)
            if not released:
                results.put(("error", os.getpid(), "release timeout"))
                return
            refresh_response = httpx.Response(
                200,
                json={
                    "access_token": f"access-{os.getpid()}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": f"refresh-{os.getpid()}",
                },
                request=first_request,
            )
            next_request = await flow.asend(refresh_response)
            results.put(
                (
                    "after_refresh",
                    os.getpid(),
                    _classify_request(next_request),
                    time.monotonic(),
                    release_refresh.is_set(),
                )
            )
        else:
            next_request = first_request

        # Complete the auth flow normally. Closing the outer wrapper while the
        # SDK generator is suspended inside its AnyIO lock can make AnyIO report
        # that shutdown is happening from a different task.
        try:
            await flow.asend(httpx.Response(200, request=next_request))
        except StopAsyncIteration:
            pass

    asyncio.run(_run())


def _lock_worker(hermes_home: str, start_barrier, release_lock, results) -> None:
    os.environ["HERMES_HOME"] = hermes_home

    async def _run() -> None:
        from tools.mcp_oauth import HermesTokenStorage

        lock = HermesTokenStorage("srv").refresh_lock()
        await asyncio.to_thread(start_barrier.wait)
        await lock.acquire()
        results.put(("acquired", os.getpid()))
        await asyncio.to_thread(release_lock.wait, 10)
        await lock.release()

    asyncio.run(_run())


def _sync_lock_worker(hermes_home: str, start_barrier, release_lock, results) -> None:
    os.environ["HERMES_HOME"] = hermes_home
    from tools.mcp_oauth import HermesTokenStorage

    start_barrier.wait()
    with HermesTokenStorage("srv").refresh_lock():
        results.put(("acquired", os.getpid()))
        release_lock.wait(10)


@pytest.mark.asyncio
async def test_refresh_lock_accepts_a_preexisting_empty_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A crash-created empty lock file must not strand future refreshes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.mcp_oauth import HermesTokenStorage

    lock_path = tmp_path / "mcp-tokens" / "srv.refresh.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()

    lock = HermesTokenStorage("srv").refresh_lock()
    await asyncio.wait_for(lock.acquire(), timeout=1.0)
    await lock.release()


def test_sync_reauthorization_lock_excludes_another_process(tmp_path: Path) -> None:
    """The synchronous reauth lease shares the async refresh lock file."""
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    release_lock = ctx.Event()
    results = ctx.Queue()
    processes = [
        ctx.Process(
            target=_sync_lock_worker,
            args=(str(tmp_path), barrier, release_lock, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()

    try:
        first = results.get(timeout=20)
        assert first[0] == "acquired"
        with pytest.raises(queue_module.Empty):
            results.get(timeout=0.75)
        release_lock.set()
        second = results.get(timeout=10)
        assert second[0] == "acquired"
    finally:
        release_lock.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)


@pytest.mark.asyncio
async def test_rejected_refresh_is_not_retried_or_promoted_to_browser_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A rejected single-use token must remain consumed and fail closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthClientMetadata,
        OAuthMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage
    from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS
    from tools.mcp_tool import sdk_httpx

    assert _HERMES_PROVIDER_CLS is not None
    httpx = sdk_httpx()
    storage = HermesTokenStorage("srv")
    await storage.set_tokens(
        OAuthToken(
            access_token="expired-access",
            token_type="Bearer",
            expires_in=0,
            refresh_token="rejected-refresh",
        )
    )
    await storage.set_client_info(
        OAuthClientInformationFull(
            client_id="test-client",
            redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        )
    )
    storage.save_oauth_metadata(
        OAuthMetadata.model_validate(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "response_types_supported": ["code"],
            }
        )
    )

    browser_calls: list[str] = []

    async def _redirect(url: str) -> None:
        browser_calls.append(url)

    async def _callback() -> tuple[str, str | None]:
        raise AssertionError("browser authorization must not run")

    def _provider():
        return _HERMES_PROVIDER_CLS(
            server_name="srv",
            server_url="https://mcp.example.com/mcp",
            client_metadata=OAuthClientMetadata(
                redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
                client_name="Hermes Agent test",
            ),
            storage=HermesTokenStorage("srv"),
            redirect_handler=_redirect,
            callback_handler=_callback,
        )

    first_provider = _provider()
    first_flow = first_provider.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    refresh_request = await first_flow.__anext__()
    assert _classify_request(refresh_request) == "refresh"
    with pytest.raises(Exception, match="explicit reauthorization"):
        await first_flow.asend(httpx.Response(400, request=refresh_request))

    same_provider_flow = first_provider.async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    with pytest.raises(Exception, match="explicit reauthorization"):
        await same_provider_flow.__anext__()

    second_flow = _provider().async_auth_flow(
        httpx.Request("POST", "https://mcp.example.com/mcp")
    )
    with pytest.raises(Exception, match="explicit reauthorization"):
        await second_flow.__anext__()
    assert browser_calls == []


def test_refresh_file_lock_excludes_another_process(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    start_barrier = ctx.Barrier(2)
    release_lock = ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_lock_worker,
            args=(str(tmp_path), start_barrier, release_lock, results),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()
    try:
        assert results.get(timeout=20)[0] == "acquired"
        with pytest.raises(queue_module.Empty):
            results.get(timeout=1.0)
        release_lock.set()
        assert results.get(timeout=20)[0] == "acquired"
    finally:
        release_lock.set()
        for worker in workers:
            worker.join(timeout=20)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)

    assert all(worker.exitcode == 0 for worker in workers)


@pytest.mark.skipif(
    os.environ.get("HERMES_TEST_NO_MULTIPROCESS") == "1",
    reason="multiprocess tests disabled by environment",
)
def test_single_use_refresh_token_is_consumed_by_only_one_process(
    tmp_path: Path,
    monkeypatch,
):
    """A second process waits, reloads, and skips refresh after rotation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    from mcp.shared.auth import (
        OAuthClientInformationFull,
        OAuthMetadata,
        OAuthToken,
    )
    from pydantic import AnyUrl

    from tools.mcp_oauth import HermesTokenStorage

    storage = HermesTokenStorage("srv")
    asyncio.run(
        storage.set_tokens(
            OAuthToken(
                access_token="expired-access",
                token_type="Bearer",
                expires_in=0,
                refresh_token="single-use-refresh",
            )
        )
    )
    asyncio.run(
        storage.set_client_info(
            OAuthClientInformationFull(
                client_id="test-client",
                redirect_uris=[AnyUrl("http://127.0.0.1:12345/callback")],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
            )
        )
    )
    storage.save_oauth_metadata(
        OAuthMetadata.model_validate(
            {
                "issuer": "https://auth.example.com",
                "authorization_endpoint": "https://auth.example.com/authorize",
                "token_endpoint": "https://auth.example.com/token",
                "response_types_supported": ["code"],
            }
        )
    )

    ctx = multiprocessing.get_context("spawn")
    start_barrier = ctx.Barrier(2)
    release_refresh = ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_refresh_worker,
            args=(str(tmp_path), start_barrier, release_refresh, results),
        )
        for _ in range(2)
    ]

    for worker in workers:
        worker.start()

    premature_second = None
    messages = []
    try:
        first = results.get(timeout=20)
        messages.append(first)
        assert first[0] == "first"
        assert first[2] == "refresh"

        # The process holding the refresh lease is deliberately paused. A
        # correct cross-process lock keeps the second process from consuming
        # the same single-use refresh token while the first is in flight.
        try:
            premature_second = results.get(timeout=1.0)
            messages.append(premature_second)
        except queue_module.Empty:
            pass

        release_refresh.set()

        while len(messages) < 3:
            messages.append(results.get(timeout=20))
    finally:
        release_refresh.set()
        for worker in workers:
            worker.join(timeout=20)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)

    assert premature_second is None, (
        "both processes issued the same single-use refresh token before the "
        f"first refresh completed: {messages}"
    )
    assert sorted(message[2] for message in messages if message[0] == "first") == [
        "mcp",
        "refresh",
    ]
    assert all(worker.exitcode == 0 for worker in workers)
