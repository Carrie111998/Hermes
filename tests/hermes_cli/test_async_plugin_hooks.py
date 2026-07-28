"""Behavior tests for asynchronous plugin hook invocation."""

import asyncio
import threading
import time
from contextvars import ContextVar

import pytest

from hermes_cli.plugins import (
    PluginContext,
    PluginHookConflictError,
    PluginManager,
    PluginManifest,
)
from hermes_constants import GATEWAY_MESSAGE_HOOK_API_VERSION


def test_plugin_context_exposes_gateway_message_capability_version():
    context = PluginContext(
        PluginManifest(name="capability-probe", source="entrypoint"),
        PluginManager(),
    )

    assert context.gateway_message_hook_api_version == GATEWAY_MESSAGE_HOOK_API_VERSION


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
async def test_async_hook_can_offload_blocking_sync_callback_for_host_timeout():
    manager = PluginManager()
    finished = False
    daemon = None

    def blocking_callback(**_kwargs):
        nonlocal daemon, finished
        daemon = threading.current_thread().daemon
        time.sleep(0.2)
        finished = True

    manager._hooks["gateway_session_cancel"] = [blocking_callback]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            manager.invoke_hook_async(
                "gateway_session_cancel",
                offload_sync=True,
            ),
            timeout=0.01,
        )

    assert daemon is True
    assert finished is False


@pytest.mark.asyncio
async def test_async_hook_keeps_async_callbacks_on_gateway_loop_when_sync_callbacks_offload():
    manager = PluginManager()
    profile = ContextVar("profile", default="default")
    seen = []
    gateway_loop = asyncio.get_running_loop()
    gateway_future = gateway_loop.create_future()

    async def async_callback(**_kwargs):
        seen.append(
            (
                profile.get(),
                threading.current_thread().daemon,
                asyncio.get_running_loop() is gateway_loop,
            )
        )
        return await gateway_future

    manager._hooks["gateway_session_cancel"] = [async_callback]
    token = profile.set("work")
    try:
        task = asyncio.create_task(
            manager.invoke_hook_async(
                "gateway_session_cancel",
                offload_callbacks=True,
            )
        )
        await asyncio.sleep(0)
        gateway_future.set_result("done")
        results = await task
    finally:
        profile.reset(token)

    assert results == ["done"]
    assert seen == [("work", False, True)]


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


@pytest.mark.parametrize(
    "order",
    [
        ("gateway_message", "pre_gateway_dispatch"),
        ("pre_gateway_dispatch", "gateway_message"),
    ],
)
def test_behavior_changing_gateway_hooks_conflict_at_configuration_time(order):
    manager = PluginManager()
    callbacks = {
        "gateway_message": lambda **_kwargs: {"decision": "pass"},
        "pre_gateway_dispatch": lambda **_kwargs: {"action": "allow"},
    }
    for index, hook_name in enumerate(order):
        context = PluginContext(
            PluginManifest(name=f"plugin-{index}", source="entrypoint"),
            manager,
        )
        context.register_hook(hook_name, callbacks[hook_name])

    with pytest.raises(
        PluginHookConflictError,
        match="gateway_message.*pre_gateway_dispatch|pre_gateway_dispatch.*gateway_message",
    ):
        manager.validate_hook_configuration()
