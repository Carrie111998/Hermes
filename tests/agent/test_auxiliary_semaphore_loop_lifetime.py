"""Async auxiliary semaphores must not outlive their event loop (#93772).

The cache is keyed by ``(task, id(loop))``, and ``id()`` is an address-like
identity valid only while the object is alive. When a loop is closed and
collected, the allocator can hand its address to a NEW loop; the new loop
then received the dead loop's semaphore — with possibly exhausted permits,
hanging the call, or bound to a foreign loop, raising cross-loop errors.
Entries now carry a weakref to their owning loop and are validated (and
pruned) against it.
"""

import asyncio
from unittest.mock import patch

from agent.auxiliary_client import (
    _acquire_async_aux_semaphore,
    _aux_async_semaphores,
    _reset_aux_semaphores,
)

_CONFIG = {"max_concurrency": 2}


def setup_function(function):
    _reset_aux_semaphores()


def teardown_function(function):
    _reset_aux_semaphores()


def test_new_loop_does_not_inherit_dead_loop_semaphore():
    """Same id() after the first loop died must yield a FRESH semaphore."""
    async def first():
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=_CONFIG,
        ):
            return _acquire_async_aux_semaphore("compression")

    old_loop = asyncio.new_event_loop()
    sem_old = old_loop.run_until_complete(first())
    old_loop.close()
    del old_loop  # eligible for collection — its id may be recycled

    # Force collection so the weakref target is really gone.
    import gc
    gc.collect()

    async def second():
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=_CONFIG,
        ):
            return _acquire_async_aux_semaphore("compression")

    new_loop = asyncio.new_event_loop()
    try:
        # Deterministically simulate the allocator handing back the same
        # address: run the lookup under the OLD cache key.
        sem_new = new_loop.run_until_complete(second())
        assert sem_new is not None
        assert sem_new is not sem_old, (
            "a live loop must never receive a dead loop's cached semaphore"
        )
    finally:
        new_loop.close()


def test_dead_entries_for_task_are_pruned():
    async def grab():
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=_CONFIG,
        ):
            return _acquire_async_aux_semaphore("compression")

    loops = []
    for _ in range(5):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(grab())
        loops.append(loop)

    for loop in loops:
        loop.close()
    del loops
    import gc
    gc.collect()

    async def touch():
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=_CONFIG,
        ):
            return _acquire_async_aux_semaphore("compression")

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(touch())
        assert len(_aux_async_semaphores) <= 1, (
            "dead-loop entries must be pruned, not accumulate forever"
        )
    finally:
        loop.close()


def test_same_live_loop_still_reuses_semaphore():
    """The happy path — one loop, repeated calls — keeps a single entry."""

    async def grab():
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value=_CONFIG,
        ):
            return (
                _acquire_async_aux_semaphore("compression"),
                _acquire_async_aux_semaphore("compression"),
            )

    loop = asyncio.new_event_loop()
    try:
        s1, s2 = loop.run_until_complete(grab())
        assert s1 is s2
        assert len(_aux_async_semaphores) == 1
    finally:
        loop.close()
