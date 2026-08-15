"""Behavior contracts for the gateway adapter-lifecycle mixin."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.adapter_lifecycle_mixin import GatewayAdapterLifecycleMixin
from gateway.config import Platform
from gateway.run import GatewayRunner


_METHOD_NAMES = (
    "_await_adapter_cleanup_with_timeout",
    "_safe_adapter_disconnect",
    "_bounded_adapter_teardown",
    "_adapter_disconnect_timeout_secs",
    "_platform_connect_timeout_secs",
    "_connect_adapter_with_timeout",
)


def test_mixin_exposes_all_lifecycle_methods():
    missing = [name for name in _METHOD_NAMES if not hasattr(GatewayAdapterLifecycleMixin, name)]
    assert not missing


def test_runner_resolves_lifecycle_methods_via_mro():
    shell = object.__new__(GatewayRunner)
    missing = [name for name in _METHOD_NAMES if not hasattr(shell, name)]
    assert not missing


def test_methods_resolve_to_mixin_not_runner_copy():
    for name in _METHOD_NAMES:
        assert getattr(GatewayRunner, name) is getattr(GatewayAdapterLifecycleMixin, name)


def test_timeout_getters_do_not_require_init_state(monkeypatch):
    shell = object.__new__(GatewayRunner)
    monkeypatch.delenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)

    assert shell._adapter_disconnect_timeout_secs() > 0
    assert shell._platform_connect_timeout_secs() > 0
    assert shell._platform_connect_timeout_secs(Platform.TELEGRAM) > 0


@pytest.mark.asyncio
async def test_connect_helper_forwards_is_reconnect_kwarg(monkeypatch):
    shell = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter.connect = AsyncMock(return_value=True)
    monkeypatch.delenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", raising=False)

    result = await shell._connect_adapter_with_timeout(
        adapter, Platform.TELEGRAM, is_reconnect=True
    )

    assert result is True
    adapter.connect.assert_awaited_once_with(is_reconnect=True)


@pytest.mark.asyncio
async def test_safe_disconnect_forwards_progress_on_partial_init():
    shell = object.__new__(GatewayRunner)
    adapter = MagicMock()
    adapter.disconnect = AsyncMock(side_effect=RuntimeError("partial init"))

    await shell._safe_adapter_disconnect(adapter, Platform.TELEGRAM)

    adapter.disconnect.assert_awaited_once()
