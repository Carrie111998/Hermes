"""Behavior tests for asynchronous plugin hook invocation."""

import asyncio

import pytest

from hermes_cli.plugins import PluginManager


@pytest.mark.asyncio
async def test_async_hook_invocation_supports_sync_and_async_callbacks():
    manager = PluginManager()
    calls = []

    def sync_callback(**kwargs):
        calls.append(("sync", kwargs["value"]))
        return {"decision": "pass"}

    async def async_callback(**kwargs):
        await asyncio.sleep(0)
        calls.append(("async", kwargs["value"]))
        return {"decision": "handled"}

    manager._hooks["gateway_message"] = [sync_callback, async_callback]

    results = await manager.invoke_hook_async("gateway_message", value="hello")

    assert calls == [("sync", "hello"), ("async", "hello")]
    assert results == [{"decision": "pass"}, {"decision": "handled"}]


@pytest.mark.asyncio
async def test_async_hook_invocation_can_stop_before_later_callbacks():
    manager = PluginManager()
    calls = []

    def handled(**_kwargs):
        calls.append("handled")
        return {"decision": "handled"}

    def must_not_run(**_kwargs):
        calls.append("later")
        return {"decision": "pass"}

    manager._hooks["gateway_message"] = [handled, must_not_run]

    results = await manager.invoke_hook_async(
        "gateway_message",
        stop_when=lambda result: result == {"decision": "handled"},
    )

    assert results == [{"decision": "handled"}]
    assert calls == ["handled"]


@pytest.mark.asyncio
async def test_async_hook_invocation_excludes_unmatched_none_results():
    manager = PluginManager()
    manager._hooks["gateway_message"] = [lambda **_kwargs: None]

    assert await manager.invoke_hook_async("gateway_message") == []


@pytest.mark.asyncio
async def test_async_hook_invocation_can_fail_fast_for_terminal_hooks():
    manager = PluginManager()

    def broken(**_kwargs):
        raise RuntimeError("broken hook")

    manager._hooks["gateway_message"] = [broken]

    with pytest.raises(RuntimeError, match="broken hook"):
        await manager.invoke_hook_async("gateway_message", raise_exceptions=True)


@pytest.mark.asyncio
async def test_async_hook_invocation_reraises_cancelled_error():
    manager = PluginManager()

    async def cancelled(**_kwargs):
        raise asyncio.CancelledError

    manager._hooks["gateway_message"] = [cancelled]

    with pytest.raises(asyncio.CancelledError):
        await manager.invoke_hook_async("gateway_message")
