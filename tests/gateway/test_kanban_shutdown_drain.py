"""Gateway-side Kanban shutdown/update drain coverage for #44877."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, patch

import pytest

from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner


def _runner() -> GatewayRunner:
    runner, _adapter = make_restart_runner()
    # Keep these tests isolated from the process-global cron scheduler and
    # adapter implementations; each case controls only Kanban lifecycle work.
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    return runner


def test_active_work_and_runtime_status_include_all_board_workers(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(
        kanban_db, "active_worker_pids_all_boards", lambda: [3101, 3102]
    )

    assert runner._active_kanban_worker_count() == 2
    assert runner._active_work_count() == 2

    with patch("gateway.status.write_runtime_status") as write_status:
        runner._persist_active_agents()

    assert write_status.call_args.kwargs["active_agents"] == 2


@pytest.mark.asyncio
async def test_drain_waits_for_kanban_workers_on_background_budget(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    active_pids = [4101]
    monkeypatch.setattr(
        kanban_db, "active_worker_pids_all_boards", lambda: list(active_pids)
    )

    async def finish_worker() -> None:
        await asyncio.sleep(0.12)
        active_pids.clear()

    finish_task = asyncio.create_task(finish_worker())
    loop = asyncio.get_running_loop()
    started = loop.time()
    _snapshot, timed_out = await runner._drain_active_agents(0.0, 1.0)
    elapsed = loop.time() - started
    await finish_task

    assert timed_out is False
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_drain_does_not_report_graceful_with_worker_still_live(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", lambda: [5101])

    loop = asyncio.get_running_loop()
    started = loop.time()
    _snapshot, timed_out = await runner._drain_active_agents(0.0, 0.12)
    elapsed = loop.time() - started

    assert timed_out is True
    assert runner._active_kanban_worker_count(fail_closed=True) == 1
    assert 0.1 <= elapsed < 1.0


@pytest.mark.asyncio
async def test_drain_probe_error_fails_closed_within_budget(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()

    def _broken_probe() -> list[int]:
        raise OSError("board WAL unreadable")

    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", _broken_probe)

    # Normal runtime status stays best-effort, but the drain must never turn
    # the same unknown state into a false zero.
    assert runner._active_kanban_worker_count(fail_closed=False) == 0
    loop = asyncio.get_running_loop()
    started = loop.time()
    _snapshot, timed_out = await runner._drain_active_agents(0.0, 0.12)
    elapsed = loop.time() - started

    assert timed_out is True
    assert runner._kanban_worker_probe_failed is True
    assert "board WAL unreadable" in runner._kanban_worker_probe_error
    assert 0.1 <= elapsed < 1.0


@pytest.mark.asyncio
async def test_kanban_termination_probe_error_fails_closed(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()

    def _broken_probe() -> list[int]:
        raise OSError("board WAL unreadable")

    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", _broken_probe)

    with pytest.raises(RuntimeError, match="environment"):
        await runner._terminate_active_kanban_workers("gateway restart")


@pytest.mark.asyncio
async def test_drain_waits_for_dispatch_admitted_just_before_quiesce(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", lambda: [])
    runner._kanban_dispatch_claim_inflight = True

    async def finish_dispatch() -> None:
        await asyncio.sleep(0.12)
        runner._kanban_dispatch_claim_inflight = False

    finish_task = asyncio.create_task(finish_dispatch())
    _snapshot, timed_out = await runner._drain_active_agents(0.0, 1.0)
    await finish_task

    assert timed_out is False


@pytest.mark.asyncio
async def test_drain_treats_locked_claim_gate_as_inflight(monkeypatch):
    """Close the tiny window before the dispatcher sets its in-flight flag."""
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", lambda: [])
    gate = runner._kanban_dispatch_gate()
    gate.acquire()
    release_gate = threading.Timer(0.12, gate.release)
    release_gate.start()

    try:
        assert runner._kanban_dispatch_claim_inflight_count() == 1
        _snapshot, timed_out = await runner._drain_active_agents(0.0, 1.0)
    finally:
        release_gate.join(timeout=2.0)
        if gate.locked():
            gate.release()

    assert timed_out is False


def test_dispatch_gate_rejects_queued_claim_after_quiesce():
    runner = _runner()
    runner._set_kanban_dispatch_quiesced(False)
    first_entered = threading.Event()
    release_first = threading.Event()
    results: list[object] = []
    dispatch_calls: list[str] = []

    def _first_dispatch() -> str:
        dispatch_calls.append("first")
        first_entered.set()
        assert release_first.wait(timeout=2.0)
        return "first-result"

    def _run_first() -> None:
        results.append(runner._kanban_dispatch_once_if_allowed(_first_dispatch))

    def _run_late() -> None:
        results.append(
            runner._kanban_dispatch_once_if_allowed(
                lambda: dispatch_calls.append("late")
            )
        )

    first = threading.Thread(target=_run_first)
    first.start()
    assert first_entered.wait(timeout=2.0)
    assert runner._kanban_dispatch_claim_inflight_count() == 1

    runner._set_kanban_dispatch_quiesced(True)
    late = threading.Thread(target=_run_late)
    late.start()
    release_first.set()
    first.join(timeout=2.0)
    late.join(timeout=2.0)

    assert not first.is_alive()
    assert not late.is_alive()
    assert dispatch_calls == ["first"]
    assert sorted(results, key=lambda item: item is None) == ["first-result", None]
    assert runner._kanban_dispatch_claim_inflight_count() == 0


def test_external_update_drain_quiesces_and_cancel_reopens_dispatch(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", lambda: [])

    runner._enter_external_drain()

    assert runner._external_drain_active is True
    assert runner._kanban_dispatch_claims_blocked() is True
    assert runner._kanban_dispatch_once_if_allowed(lambda: "claimed") is None

    runner._exit_external_drain()

    assert runner._external_drain_active is False
    assert runner._kanban_dispatch_claims_blocked() is False
    assert runner._kanban_dispatch_once_if_allowed(lambda: "claimed") == "claimed"


@pytest.mark.asyncio
async def test_restart_quiesces_dispatch_before_after_turn_wait(monkeypatch):
    from hermes_cli import kanban_db

    runner = _runner()
    monkeypatch.setattr(kanban_db, "active_worker_pids_all_boards", lambda: [])
    runner._await_active_work_before_restart = AsyncMock(return_value=True)
    runner.stop = AsyncMock()

    assert runner.request_restart() is True
    assert runner._kanban_dispatch_claims_blocked() is True
    assert runner._kanban_dispatch_once_if_allowed(lambda: "claimed") is None

    await runner._restart_task
    runner.stop.assert_awaited_once()
