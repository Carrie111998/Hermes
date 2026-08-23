"""A cancelled apply_session_options() task cannot leave disk ahead of live.

The durable write runs in a worker thread (AsyncSessionStore -> to_thread);
cancelling the awaiting task does not un-write the file. The primitive must
therefore finish the persist+assign unit before propagating the cancellation,
so live SessionState always matches what landed on disk.
"""
import asyncio
import threading

import pytest

from tests.gateway.test_session_options_rejections import (
    _make_runner,
    _make_source,
    store,  # noqa: F401  (fixture re-export)
)


@pytest.mark.asyncio
async def test_cancel_during_persist_still_assigns_live_to_match_disk(store, monkeypatch):
    runner = _make_runner(store)
    source = _make_source()
    key = runner._session_key_for_source(runner._normalize_source_for_session_key(source))
    store.get_or_create_session(source)

    entered = threading.Event()
    release = threading.Event()
    real_save = store._save_sessions_json

    def _slow_save(data):
        # get_or_create_session also saves (last_active); only park the save
        # that carries the runtime-options write.
        if any(
            (entry or {}).get("reasoning_override") for entry in data.values()
        ):
            entered.set()
            assert release.wait(5), "test never released the write"
        return real_save(data)

    monkeypatch.setattr(store, "_save_sessions_json", _slow_save)

    task = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    # Park the task inside the worker-thread write, then cancel it.
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    durable = (store.get_runtime_options(key) or {}).get("reasoning_override")
    live = runner._session_state(key).conversation.reasoning_override
    assert durable == live, f"disk {durable!r} != live {live!r}"
    assert durable == {"enabled": True, "effort": "high"}
    # The lock was released on the way out: a follow-up call is not wedged.
    again = await runner.apply_session_options(source, {"reasoning_effort": "low"})
    assert again["status"] == "accepted"
