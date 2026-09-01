"""Event-loop teardown cannot separate the durable write from live assignment.

``asyncio.run`` teardown cancels every task (``_cancel_all_tasks``). Live
assignment is a done-callback on the write's executor Future -- not a task --
so the caller, absorbing that cancel under the lock, still observes live
state follow whatever landed on disk before the loop closes.
"""

import asyncio
import threading

from tests.gateway.test_session_options_cancel_mid_persist import (
    _key,
    _park_runtime_options_save,
)
from tests.gateway.test_session_options_rejections import (
    _make_runner,
    _make_source,
    store,  # noqa: F401  (fixture re-export)
)


def test_loop_teardown_cannot_separate_write_from_live_assignment(store, monkeypatch):
    runner = _make_runner(store)
    source = _make_source()
    key = _key(runner, source)
    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)

    async def main() -> asyncio.Task:
        task = asyncio.create_task(
            runner.apply_session_options(source, {"reasoning_effort": "high"})
        )
        await asyncio.to_thread(entered.wait, 5)
        # Return with the write still parked in the worker: asyncio.run now
        # cancels every task, then joins the default executor.
        threading.Timer(0.2, release.set).start()
        return task

    task = asyncio.run(main())

    assert task.cancelled()
    durable = (store.get_runtime_options(key) or {}).get("reasoning_override")
    live = runner._session_state(key).conversation.reasoning_override
    assert durable == {"enabled": True, "effort": "high"}
    assert durable == live, f"disk {durable!r} != live {live!r}"
