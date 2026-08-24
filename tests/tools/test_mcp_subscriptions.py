from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import tools.mcp_tool as mcp_tool
from mcp.client.subscriptions import (
    PromptsListChanged,
    ResourcesListChanged,
    SubscriptionLost,
    ToolsListChanged,
)
from mcp.shared.exceptions import MCPError


def _run(coro):
    return asyncio.run(coro)


class _FakeSubscription:
    def __init__(self, events=(), *, error=None, blocker=None, honored=None):
        self._events = deque(events)
        self._error = error
        self._blocker = blocker
        if honored is not None:
            self.honored = honored

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.popleft()
        if self._error is not None:
            error = self._error
            self._error = None
            raise error
        if self._blocker is not None:
            await self._blocker.wait()
        raise StopAsyncIteration


class _CatalogueSession:
    protocol_version = "2026-07-28"

    def __init__(self):
        self.catalogue_calls = []
        self.catalogue_event = asyncio.Event()

    async def list_prompts(self, **_kwargs):
        self.catalogue_calls.append("prompts")
        self.catalogue_event.set()
        return SimpleNamespace(prompts=[], nextCursor=None)

    async def list_resources(self, **_kwargs):
        self.catalogue_calls.append("resources")
        self.catalogue_event.set()
        return SimpleNamespace(resources=[], nextCursor=None)


def _modern_server(name="subscription-test"):
    server = mcp_tool.MCPServerTask(name)
    server._connection_generation = 1
    server.negotiated_era = "modern"
    server.negotiated_protocol_version = "2026-07-28"
    server.session = _CatalogueSession()
    server.initialize_result = SimpleNamespace(
        capabilities=SimpleNamespace(
            tools=SimpleNamespace(),
            prompts=SimpleNamespace(),
            resources=SimpleNamespace(),
        )
    )
    return server


def _install_listen(monkeypatch, subscriptions, calls, entered=None):
    queue = deque(subscriptions)

    @asynccontextmanager
    async def fake_listen(_session, **kwargs):
        calls.append(kwargs)
        if entered is not None:
            entered.set()
        yield queue.popleft()

    monkeypatch.setattr("mcp.client.subscriptions.listen", fake_listen)


def test_subscription_has_one_owner_per_generation(monkeypatch):
    async def drive():
        blocker = asyncio.Event()
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription(blocker=blocker)],
            calls,
        )
        server = _modern_server()
        first = server._start_subscription_supervisor(1)
        second = server._start_subscription_supervisor(1)
        await asyncio.sleep(0)
        assert first is second
        assert len(calls) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

    _run(drive())


def test_subscription_event_refreshes_tools_and_delivers_sequentially(monkeypatch):
    async def drive():
        blocker = asyncio.Event()
        calls = []
        refreshed = asyncio.Event()
        _install_listen(
            monkeypatch,
            [_FakeSubscription([ToolsListChanged()], blocker=blocker)],
            calls,
        )
        server = _modern_server()
        refresh_generations = []

        async def refresh(_server, *, generation):
            refresh_generations.append(generation)
            refreshed.set()

        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh)
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        assert refresh_generations == [1]
        assert server.subscription_state == "active"
        assert server.catalogue_state == "current"
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    _run(drive())


@pytest.mark.parametrize(
    ("event", "expected_family"),
    [
        (PromptsListChanged(), "prompts"),
        (ResourcesListChanged(), "resources"),
    ],
)
def test_subscription_event_reconciles_non_tool_catalogue(
    monkeypatch, event, expected_family
):
    async def drive():
        blocker = asyncio.Event()
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription([event], blocker=blocker)],
            calls,
        )
        server = _modern_server()
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(server.session.catalogue_event.wait(), timeout=1)
        assert server.session.catalogue_calls == [expected_family]
        assert server.catalogue_state == "current"
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    _run(drive())


@pytest.mark.parametrize("first_error", [None, SubscriptionLost("dropped")])
def test_subscription_gap_relists_and_reconciles_catalogue(monkeypatch, first_error):
    async def drive():
        blocker = asyncio.Event()
        calls = []
        refreshed = asyncio.Event()
        first = _FakeSubscription(error=first_error)
        second = _FakeSubscription(blocker=blocker)
        _install_listen(monkeypatch, [first, second], calls)
        server = _modern_server()
        observed_states = []

        async def refresh(_server, *, generation):
            observed_states.append((generation, server.catalogue_state))
            refreshed.set()

        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh)
        monkeypatch.setattr(mcp_tool, "_SUBSCRIPTION_RELISTEN_DELAY", 0)
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(refreshed.wait(), timeout=1)
        assert len(calls) == 2
        assert observed_states == [(1, "stale")]
        assert server.session.catalogue_calls == ["prompts", "resources"]
        assert server.catalogue_state == "current"
        assert server.subscription_state == "active"
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    _run(drive())


def test_subscription_relisten_exhaustion_escalates_to_reconnect(monkeypatch):
    async def drive():
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription(), _FakeSubscription(), _FakeSubscription()],
            calls,
        )
        server = _modern_server()

        async def refresh(_server, *, generation):
            assert generation == 1

        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh)
        monkeypatch.setattr(mcp_tool, "_MAX_SUBSCRIPTION_RELISTENS", 2)
        monkeypatch.setattr(mcp_tool, "_SUBSCRIPTION_RELISTEN_DELAY", 0)
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(listener, timeout=1)
        assert len(calls) == 3
        assert server.subscription_state == "exhausted"
        assert server.catalogue_state == "stale"
        assert server._reconnect_event.is_set()

    _run(drive())


@pytest.mark.parametrize(
    "failure",
    [
        mcp_tool._JSONRPC_METHOD_NOT_FOUND,
        "version",
    ],
)
def test_subscription_not_supported_is_bounded(monkeypatch, failure):
    async def drive():
        calls = []

        @asynccontextmanager
        async def rejected(_session, **kwargs):
            calls.append(kwargs)
            if failure == "version":
                from mcp.client.subscriptions import ListenNotSupportedError

                raise ListenNotSupportedError("2025-11-25")
            raise MCPError(code=failure, message="Method not found")
            yield

        monkeypatch.setattr("mcp.client.subscriptions.listen", rejected)
        server = _modern_server()
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(listener, timeout=1)
        assert len(calls) == 1
        assert server.subscription_state == "unsupported"
        assert not server._reconnect_event.is_set()

    _run(drive())


def test_subscription_empty_ack_is_unsupported_without_reconnect(monkeypatch):
    async def drive():
        calls = []
        honored = SimpleNamespace(
            tools_list_changed=None,
            prompts_list_changed=None,
            resources_list_changed=None,
        )
        _install_listen(
            monkeypatch,
            [_FakeSubscription(honored=honored)],
            calls,
        )
        server = _modern_server()
        monkeypatch.setattr(mcp_tool, "_MAX_SUBSCRIPTION_RELISTENS", 0)
        listener = server._start_subscription_supervisor(1)
        await asyncio.wait_for(listener, timeout=1)
        assert len(calls) == 1
        assert server.subscription_state == "unsupported"
        assert not server._reconnect_event.is_set()

    _run(drive())


def test_subscription_caller_cancellation_cleans_owner(monkeypatch):
    async def drive():
        blocker = asyncio.Event()
        entered = asyncio.Event()
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription(blocker=blocker)],
            calls,
            entered,
        )
        server = _modern_server()
        listener = server._start_subscription_supervisor(1)
        await entered.wait()
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
        await asyncio.sleep(0)
        assert server._listen_task is None
        assert server._listen_generation is None
        assert server.subscription_state == "stopped"

    _run(drive())


def test_generation_replacement_cancels_listener_and_resets_state(monkeypatch):
    async def drive():
        blocker = asyncio.Event()
        entered = asyncio.Event()
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription(blocker=blocker)],
            calls,
            entered,
        )
        server = _modern_server()
        listener = server._start_subscription_supervisor(1)
        await entered.wait()
        generation = await server._begin_connection_generation()
        assert generation == 2
        assert listener.cancelled()
        assert server._listen_task is None
        assert server._listen_generation is None
        assert server.subscription_state == "idle"
        assert server.catalogue_state == "stale"

    _run(drive())


def test_old_generation_listener_cannot_refresh_current_catalogue(monkeypatch):
    async def drive():
        release = asyncio.Event()
        calls = []
        _install_listen(
            monkeypatch,
            [_FakeSubscription([ToolsListChanged()], blocker=release)],
            calls,
        )
        server = _modern_server()
        refreshed = []

        async def refresh(_server, *, generation):
            refreshed.append(generation)

        monkeypatch.setattr(mcp_tool.MCPServerTask, "_refresh_tools", refresh)
        server._connection_generation = 2
        server.subscription_state = "idle"
        server.catalogue_state = "current"
        await server._subscription_supervisor(1, server.session)
        assert refreshed == []
        assert server.subscription_state == "idle"
        assert server.catalogue_state == "current"

    _run(drive())
