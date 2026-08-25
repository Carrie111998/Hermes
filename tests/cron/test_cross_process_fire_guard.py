"""Guard #5 — cross-process same-job concurrency on the TICK path (2026-08-25).

Guard #3 (``_in_flight``) is a plain dict under a ``threading.Lock``, so it is
blind to a fire running in another process. That is the gap the layered
fire-claim gate did NOT close: ``claim_job_for_fire`` is only reached by the
manual (``_execute_job_now``) and external-provider (``fire_due``) paths, while
the built-in ticker takes no fire claim at all and runs its own
``_process_job``. So the ticker could start a job that ``hermes cron run`` was
already running — the ``source='builtin'`` overlapping ``source='direct'``
shape in ``executions.db``.

Guard #5 asks the durable execution ledger, which is cross-process and
PID-recycle-safe, and refuses only on POSITIVE proof of life so it can never
leave a job permanently unfireable.

Every "a live run blocks this" case here drives a REAL second process via
``live_foreign_run``; a row opened in the pytest process would be excluded by
design (see ``live_execution_for_job``) and would prove nothing.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from tests.cron._fire_claim_helpers import live_foreign_run


JOB_ID = "b74186b2eaa5"
JOB_NAME = "jobflow-matcher"


@pytest.fixture
def tick_env(tmp_path, monkeypatch):
    """Isolate the tick lock, the execution ledger, and both dedup registries.

    Without the ledger redirect a tick in this test would write rows into the
    developer's real ``executions.db``.
    """
    import cron.executions as executions
    from cron import scheduler as sch

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db"
    )
    sch._in_flight.clear()
    sch._running_job_ids.discard(JOB_ID)
    # tick() takes an exclusive file lock derived from this hook; under xdist a
    # shared path makes lock-losers short-circuit before _process_job.
    with patch("cron.scheduler._hermes_home", tmp_path):
        yield tmp_path / "cron" / "executions.db"
    sch._in_flight.clear()
    sch._running_job_ids.discard(JOB_ID)


def _run_tick(emitter, run_job_calls):
    """Drive one tick for JOB_ID with every side effect stubbed."""
    def _fake_run_job(job):
        run_job_calls.append(job)
        return (True, "# output", "response", None)

    job = {"id": JOB_ID, "name": JOB_NAME, "deliver": "local"}
    with patch("cron.scheduler.get_due_and_skipped_jobs",
               return_value=([dict(job)], [])), \
         patch("cron.scheduler.advance_next_run"), \
         patch("cron.scheduler._get_event_emitter", return_value=emitter), \
         patch("cron.scheduler.run_job", side_effect=_fake_run_job), \
         patch("cron.scheduler.save_job_output", return_value="/tmp/out.md"), \
         patch("cron.scheduler._deliver_result", return_value=None), \
         patch("cron.scheduler.mark_job_run"), \
         patch("cron.jobs.load_jobs",
               return_value=[{"id": JOB_ID, "consecutive_errors": 0}]):
        from cron.scheduler import tick
        tick(verbose=False)


def _emitter_with_capture():
    emitter = MagicMock()
    emitter.on_job_started.return_value = "started-evt"
    skips: list = []

    def _capture(**kwargs):
        skips.append(kwargs)
        return "skip-evt"

    emitter.on_job_skipped_duplicate.side_effect = _capture
    return emitter, skips


def test_tick_is_blocked_by_a_live_run_in_another_process(tick_env):
    """The gap this guard exists to close.

    A foreign process holds an open row for the job; the ticker must not start
    a second run of it, and must say so on the event bus.
    """
    emitter, skips = _emitter_with_capture()
    run_job_calls: list = []

    with live_foreign_run(tick_env, JOB_ID) as worker_pid:
        _run_tick(emitter, run_job_calls)

    assert run_job_calls == [], (
        f"tick must not run a job already live elsewhere; got {run_job_calls!r}"
    )
    # A blocked fire must not announce itself as started.
    assert emitter.on_job_started.call_count == 0

    assert len(skips) == 1, f"expected exactly one skip event, got {skips!r}"
    skip = skips[0]
    assert skip["job_id"] == JOB_ID
    assert skip["job_name"] == JOB_NAME
    assert skip["reason"] == "cross_process_fire_blocked"
    # Unavailable by construction: that id lives in the OTHER process's
    # _InFlightRecord and is never written to the ledger.
    assert skip["prior_cron_started_event_id"] is None
    assert skip["prior_elapsed_seconds"] is None or (
        skip["prior_elapsed_seconds"] >= 0
    )
    assert worker_pid  # the block really was attributable to a live foreign pid


def test_tick_runs_once_the_foreign_owner_is_dead(tick_env):
    """Anti-wedge. The guard must not outlive the run it is protecting.

    Same ledger, same job — the row is still there and still non-terminal, only
    its owner has died. Proving both arms with one open row is what
    distinguishes a working guard from one stuck on "no".
    """
    import cron.executions as executions

    emitter, skips = _emitter_with_capture()

    with live_foreign_run(tick_env, JOB_ID):
        pass  # opened, then killed on context exit

    open_rows = [
        r for r in executions.list_executions(job_id=JOB_ID)
        if r["status"] in ("claimed", "running")
    ]
    assert len(open_rows) == 1, "the dead owner's row must still be non-terminal"

    run_job_calls: list = []
    _run_tick(emitter, run_job_calls)

    assert len(run_job_calls) == 1, "a dead owner must not block the tick"
    assert skips == []


def test_the_in_flight_slot_is_released_on_a_cross_process_reject(tick_env):
    """A reject must not leave Guard #3's slot held.

    Guard #5 runs AFTER ``_try_register_in_flight`` has already taken the slot.
    Forgetting to release it would make the rejected fire block the next
    legitimate one — turning a duplicate-fire fix into a missed-fire bug. This
    is the same release the min-interval guard performs.
    """
    from cron import scheduler as sch

    emitter, skips = _emitter_with_capture()
    run_job_calls: list = []

    with live_foreign_run(tick_env, JOB_ID):
        _run_tick(emitter, run_job_calls)
        assert len(skips) == 1, "precondition: the fire must have been rejected"
        assert JOB_ID not in sch._in_flight, (
            "cross-process reject leaked the in-flight slot; the next "
            f"legitimate fire would be blocked. _in_flight={sch._in_flight!r}"
        )


def test_a_cross_process_reject_closes_its_own_execution_row(tick_env):
    """The rejected attempt must not be left dangling as claimed/running.

    ``_submit_with_guard`` stamps a row before ``_process_job`` runs. Leaving it
    open would make the recovery classifier report a phantom interrupted run —
    and, with this very guard reading that table, could block later fires.
    """
    import cron.executions as executions

    emitter, skips = _emitter_with_capture()
    run_job_calls: list = []

    with live_foreign_run(tick_env, JOB_ID) as worker_pid:
        _run_tick(emitter, run_job_calls)
        assert len(skips) == 1

        rows = executions.list_executions(job_id=JOB_ID)
        ours = [r for r in rows if r["pid"] != worker_pid]
        # Whatever row this tick stamped for itself must be terminal.
        assert all(r["status"] not in ("claimed", "running") for r in ours), (
            f"tick left its own row non-terminal: {ours!r}"
        )


def test_an_own_process_row_does_not_block_the_tick(tick_env):
    """In-process concurrency is Guard #3's job, not Guard #5's.

    A row owned by this process has a live pid by definition, and recovery
    never cleans own-process rows — so if Guard #5 counted them, one stale row
    would wedge the job for the lifetime of the gateway. It must fall through
    to Guard #3, which is exact rather than inferred.
    """
    import cron.executions as executions

    stale = executions.create_execution(JOB_ID, source="builtin")
    executions.mark_execution_running(stale["id"])

    emitter, skips = _emitter_with_capture()
    run_job_calls: list = []
    _run_tick(emitter, run_job_calls)

    assert len(run_job_calls) == 1, (
        "an own-process row must not block the tick — that is the wedge this "
        "exclusion exists to prevent"
    )
    assert skips == []


def test_a_ledger_fault_degrades_to_guard_3_instead_of_killing_the_tick(
    tick_env,
):
    """A raise here must never cost the gateway its scheduler.

    The 2026-08-11 5h08m outage was an exception escaping on this path.
    """
    emitter, skips = _emitter_with_capture()
    run_job_calls: list = []

    def _boom(_job_id):
        raise RuntimeError("ledger unavailable")

    with patch("cron.scheduler.live_execution_for_job", side_effect=_boom):
        _run_tick(emitter, run_job_calls)

    assert len(run_job_calls) == 1, "a ledger fault must not stop the tick"
    assert skips == []
