"""The e2e harness must name why a send never happened.

``BasePlatformAdapter.handle_message`` returns as soon as it has spawned its
background tasks, so an exception raised in one of them belongs to that task
and nothing re-raises it. Without this, every such failure surfaced as
``Expected 'mock' to have been called once. Called 0 times.`` — identical
whether the handler declined to reply, ran long, or crashed, which is why
intermittent e2e failures have been so hard to place.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from gateway.config import Platform
from tests.e2e.conftest import send_and_capture


class _Adapter:
    def __init__(self, worker=None):
        self.send = AsyncMock()
        self._worker = worker

    async def handle_message(self, event):
        if self._worker is not None:
            asyncio.get_running_loop().create_task(self._worker(self))


@pytest.mark.asyncio
async def test_a_crashing_background_task_is_named():
    async def _boom(_adapter):
        raise RuntimeError("simulated background crash")

    with pytest.raises(AssertionError) as excinfo:
        await send_and_capture(_Adapter(_boom), "hi", Platform.TELEGRAM)

    assert "simulated background crash" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RuntimeError), (
        "the original exception must stay chained so the traceback survives"
    )


@pytest.mark.asyncio
async def test_a_handler_that_simply_declines_is_not_reported_as_a_crash():
    """No send and no exception is a legitimate outcome for some commands.

    The helper must stay silent here — the caller's own assertion decides
    whether a missing send is a failure.
    """
    adapter = _Adapter()
    send = await send_and_capture(adapter, "hi", Platform.TELEGRAM)
    assert send is adapter.send
    assert not send.called


@pytest.mark.asyncio
async def test_a_successful_send_is_returned_untouched():
    async def _reply(adapter):
        await adapter.send("chat", "hello")

    adapter = _Adapter(_reply)
    send = await send_and_capture(adapter, "hi", Platform.TELEGRAM)
    send.assert_called_once()


@pytest.mark.asyncio
async def test_the_loop_exception_handler_is_restored():
    """The helper installs one for the poll window only."""
    loop = asyncio.get_running_loop()
    sentinel = loop.get_exception_handler()

    async def _boom(_adapter):
        raise RuntimeError("x")

    with pytest.raises(AssertionError):
        await send_and_capture(_Adapter(_boom), "hi", Platform.TELEGRAM)

    assert loop.get_exception_handler() is sentinel
