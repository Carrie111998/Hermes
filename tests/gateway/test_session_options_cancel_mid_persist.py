"""A cancelled apply_session_options() task cannot leave disk ahead of live.

The durable write runs in a worker thread (AsyncSessionStore -> to_thread);
cancelling the awaiting task does not un-write the file. The primitive must
therefore settle the persist+assign unit -- under the admission lock, across
arbitrarily repeated cancellation -- before propagating the cancellation, so
live SessionState always matches what landed on disk and no competing
admission can enter while the two are divergent.
"""
import asyncio
import logging
import threading
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session_options import session_admission_lock
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source
from tests.gateway.test_session_options_rejections import (
    _make_runner,
    _make_source,
    store,  # noqa: F401  (fixture re-export)
)


def _key(runner, source):
    return runner._session_key_for_source(runner._normalize_source_for_session_key(source))


def _park_runtime_options_save(store, monkeypatch):
    """Block the save that carries a runtime-options write until released."""
    entered = threading.Event()
    release = threading.Event()
    real_save = store._save_sessions_json

    def _slow_save(data):
        # get_or_create_session also saves (last_active); only park the save
        # that carries the runtime-options write.
        if any((entry or {}).get("reasoning_override") for entry in data.values()):
            entered.set()
            assert release.wait(5), "test never released the write"
        return real_save(data)

    monkeypatch.setattr(store, "_save_sessions_json", _slow_save)
    return entered, release


async def _settle_loop(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_during_persist_still_assigns_live_to_match_disk(store, monkeypatch):
    runner = _make_runner(store)
    source = _make_source()
    key = _key(runner, source)
    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)

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


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_admission_lock_until_unit_settles(
    store, monkeypatch
):
    """Cancel once, cancel again while settling (and again), admit a competitor:
    the lock stays held and live stays untouched until persist+assign finish."""
    runner = _make_runner(store)
    source = _make_source()
    key = _key(runner, source)
    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)
    lock = session_admission_lock(runner, key)

    apply = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.to_thread(entered.wait, 5)
    assert lock.locked()

    # Cancel #1 (shielded await), then #2 and #3 while the caller is settling.
    for _ in range(3):
        apply.cancel()
        await _settle_loop()
        assert not apply.done(), "caller surfaced cancellation before settlement"
        assert lock.locked(), "admission lock released with the unit in flight"

    # A competing admission arrives while the write is still blocked: it must
    # park on the lock, not enter.
    competitor = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "low"})
    )
    await _settle_loop()
    assert not competitor.done(), "competing admission entered under an unsettled commit"
    # (No sync store read here: the parked writer holds the store lock.)
    assert runner._session_state(key).conversation.reasoning_override is None

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await apply
    assert apply.cancelled()

    # The competitor validated against the pre-commit live triple and entered
    # only after persist+assign were terminal, so its in-lock CAS sees the
    # settled change and rejects with `conflict` -- never a commit on top of a
    # half-applied unit. A re-read retry then lands on the settled state.
    result = await competitor
    assert (result["status"], result["code"]) == ("rejected", "conflict")
    durable = (store.get_runtime_options(key) or {}).get("reasoning_override")
    live = runner._session_state(key).conversation.reasoning_override
    assert durable == live == {"enabled": True, "effort": "high"}
    retry = await runner.apply_session_options(source, {"reasoning_effort": "low"})
    assert retry["status"] == "accepted"
    durable = (store.get_runtime_options(key) or {}).get("reasoning_override")
    live = runner._session_state(key).conversation.reasoning_override
    assert durable == live == {"enabled": True, "effort": "low"}
    assert not lock.locked()


@pytest.mark.asyncio
async def test_durable_write_failure_behind_cancellation_is_observed(
    store, monkeypatch, caplog
):
    """Cancelled caller + failing write: live untouched, failure logged (not an
    unobserved task exception), caller still ends cancelled, lock released."""
    runner = _make_runner(store)
    source = _make_source()
    key = _key(runner, source)
    store.get_or_create_session(source)
    entered = threading.Event()
    release = threading.Event()

    def _failing_save(data):
        if any((entry or {}).get("reasoning_override") for entry in data.values()):
            entered.set()
            assert release.wait(5)
            raise OSError(28, "No space left on device")
        return None

    monkeypatch.setattr(store, "_save_sessions_json", _failing_save)

    apply = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.to_thread(entered.wait, 5)
    apply.cancel()
    await _settle_loop()
    apply.cancel()
    release.set()
    with caplog.at_level(logging.WARNING, logger="gateway.session_options"):
        with pytest.raises(asyncio.CancelledError):
            await apply
    assert apply.cancelled()
    assert any("failed while the caller was cancelled" in r.getMessage() for r in caplog.records)

    assert runner._session_state(key).conversation.reasoning_override is None
    assert (store.get_runtime_options(key) or {}).get("reasoning_override") is None
    assert not session_admission_lock(runner, key).locked()


@pytest.mark.asyncio
async def test_external_cancel_during_settlement_survives_enclosing_asyncio_timeout(
    store, monkeypatch
):
    """apply_session_options under asyncio.timeout: the timeout fires (cancel #1,
    absorbed by the settle loop), then an external cancel #2 arrives while the
    write is still parked. The settle loop must NOT uncancel(): the task's
    cancelling() count stays 2, so timeout.__aexit__ sees a cancel it did not
    initiate and lets CancelledError propagate. The caller ends cancelled,
    never resumes with TimeoutError."""
    runner = _make_runner(store)
    source = _make_source()
    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)
    loop = asyncio.get_running_loop()
    resumed = asyncio.Event()
    tm = asyncio.timeout(60)

    async def caller():
        try:
            async with tm:
                await runner.apply_session_options(source, {"reasoning_effort": "high"})
        except TimeoutError:
            pass
        resumed.set()
        return "resumed"

    task = asyncio.create_task(caller())
    await asyncio.to_thread(entered.wait, 5)
    tm.reschedule(loop.time())  # timeout -> cancel #1 (absorbed, still settling)
    await _settle_loop()
    assert not task.done()
    assert task.cancelling() == 1
    task.cancel()  # external cancel #2 during settlement
    assert task.cancelling() == 2
    await _settle_loop()
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert not resumed.is_set()


# ---------------------------------------------------------------------------
# (e) competing REAL inbound turn (_handle_message) parks behind settlement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_cancellation_parks_real_inbound_turn_until_unit_settles(
    store, monkeypatch
):
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="park-chat")
    session_key = runner._session_key_for_source(source)

    # Real store + the commit machinery on top of the restart harness.
    runner.session_store = store
    runner._session_options_locks = {}
    runner._session_db = None
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "old-model",
        {"provider": "openrouter", "base_url": "", "api_key": ""},
    )
    runner._evict_cached_agent = lambda _session_key: None

    runner._handle_message = GatewayRunner._handle_message.__get__(runner, GatewayRunner)
    runner._release_running_agent_state = (
        GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    )
    runner._check_slash_access = lambda *a, **k: None
    runner._begin_session_run_generation = lambda session_key: 1
    runner._is_session_run_current = lambda session_key, generation: True
    runner._invalidate_session_run_generation = lambda *a, **k: 0
    runner._claim_active_session_slot = lambda session_key, source: (object(), None)
    runner._active_session_leases = {}
    runner._busy_ack_ts = {}
    runner._post_turn_goal_continuation = AsyncMock()
    runner._is_user_authorized = lambda _source: True
    agent_runs: list[str] = []

    async def _fake_run(event, source, _quick_key, run_generation):
        agent_runs.append(_quick_key)
        return "OK"

    runner._handle_message_with_agent = _fake_run
    adapter.set_message_handler(runner._handle_message)
    adapter.send = AsyncMock()
    adapter._keep_typing = AsyncMock()
    adapter._stop_typing_refresh = AsyncMock()
    adapter._send_with_retry = AsyncMock(return_value=SendResult(success=True, message_id="1"))
    adapter._run_processing_hook = AsyncMock()

    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)
    lock = session_admission_lock(runner, session_key)

    apply = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.to_thread(entered.wait, 5)
    assert lock.locked()

    for _ in range(2):
        apply.cancel()
        await _settle_loop()
        assert not apply.done()
        assert lock.locked()

    inbound = MessageEvent(text="hello", message_type=MessageType.TEXT, source=source)
    turn = asyncio.create_task(runner._handle_message(inbound))
    await _settle_loop()
    assert not turn.done(), "real inbound turn admitted under an unsettled commit"
    assert agent_runs == []
    assert runner._is_session_running(session_key) is False
    assert runner._session_state(session_key).conversation.reasoning_override is None

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await apply
    assert apply.cancelled()
    assert await asyncio.wait_for(turn, timeout=2.0) == "OK"
    assert agent_runs == [session_key]
    durable = (store.get_runtime_options(session_key) or {}).get("reasoning_override")
    live = runner._session_state(session_key).conversation.reasoning_override
    assert durable == live == {"enabled": True, "effort": "high"}
    assert not lock.locked()


# ---------------------------------------------------------------------------
# (a) cancel landing in the same iteration the unit completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_in_the_iteration_the_unit_completes(store, monkeypatch):
    """Release the write, then cancel on the very next loop step so the cancel
    races the shield's inner-done callback; live must equal disk and the lock
    must be free afterwards."""
    runner = _make_runner(store)
    source = _make_source()
    key = _key(runner, source)
    store.get_or_create_session(source)
    entered, release = _park_runtime_options_save(store, monkeypatch)
    lock = session_admission_lock(runner, key)
    apply = asyncio.create_task(
        runner.apply_session_options(source, {"reasoning_effort": "high"})
    )
    await asyncio.to_thread(entered.wait, 5)

    release.set()
    # Cancel on EVERY loop step until the caller is terminal, so one cancel
    # lands in the very iteration the unit completes.
    while not apply.done():
        apply.cancel()
        await asyncio.sleep(0)
    assert apply.cancelled()
    durable = (store.get_runtime_options(key) or {}).get("reasoning_override")
    live = runner._session_state(key).conversation.reasoning_override
    assert durable == live == {"enabled": True, "effort": "high"}
    assert not lock.locked()
