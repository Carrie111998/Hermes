"""Fire-and-forget tasks must be tracked: the event loop keeps only weak
references to tasks, so an unreferenced sleeping task can be garbage-collected
mid-wait. Before the fix, _schedule_ephemeral_delete (and the platform
adapters' inline create_task calls) could be silently reaped, so ephemeral
messages were never deleted, inbound WeChat/QQ messages lost, and Yuanbao's
reconnect never fired.
"""
import asyncio

import pytest

from gateway.platforms.base import BasePlatformAdapter


class _MinimalAdapter(BasePlatformAdapter):
    """Concrete stand-in satisfying the abstract surface."""

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, text, **kwargs):  # noqa: ANN001, ANN003
        pass


def _bare_adapter() -> BasePlatformAdapter:
    adapter = object.__new__(_MinimalAdapter)
    adapter._background_tasks = set()
    return adapter


@pytest.mark.asyncio
async def test_schedule_ephemeral_delete_tracks_task():
    adapter = _bare_adapter()
    deleted = []

    async def fake_delete(chat_id, message_id):
        deleted.append((chat_id, message_id))

    object.__setattr__(adapter, "delete_message", fake_delete)

    adapter._schedule_ephemeral_delete("chat-1", "msg-9", ttl_seconds=1)
    assert len(adapter._background_tasks) == 1, (
        "task must be retained while pending — an unreferenced sleeping task "
        "can be GC-reaped and the delete silently dropped"
    )
    await asyncio.sleep(1.15)
    assert deleted == [("chat-1", "msg-9")]
    assert len(adapter._background_tasks) == 0, "done callback must discard"


@pytest.mark.asyncio
async def test_schedule_ephemeral_delete_survives_intermediate_gc_pressure():
    """Even with GC churn between scheduling and firing, the strong ref in
    _background_tasks keeps the pending delete alive."""
    adapter = _bare_adapter()
    deleted = []

    async def fake_delete(chat_id, message_id):
        deleted.append(message_id)

    object.__setattr__(adapter, "delete_message", fake_delete)
    adapter._schedule_ephemeral_delete("c", "m", ttl_seconds=1)

    for _ in range(3):
        garbage = [{"blob": b"x" * 4096} for _ in range(2000)]
        await asyncio.sleep(0)
        del garbage
        import gc
        gc.collect()

    assert len(adapter._background_tasks) == 1
    await asyncio.sleep(1.05)
    assert deleted == ["m"]
