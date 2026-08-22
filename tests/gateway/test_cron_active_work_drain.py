"""Tests for #60432: the gateway shutdown drain was structurally blind to
in-flight cron work. Cron jobs run through cron/scheduler.py's own thread
pool, entirely outside ``GatewayRunner._running_agents`` -- the dict every
other active-work check on this class reads. A shutdown (``/update``,
``/restart``, SIGUSR1 -- they all funnel through the same ``stop()``) could
report ``active_at_start=0`` and immediately kill tool subprocesses while a
cron job's terminal command was still running.

These tests cover the gateway side of the fix:
  - _active_cron_job_count() reads cron.scheduler's in-flight job set
  - _drain_active_agents() waits for cron work the same way it already
    waits for chat sessions
  - the final tool-subprocess kill marks any still-in-flight cron job
    interrupted

See tests/cron/test_shutdown_interrupt.py for the cron-side primitives
this relies on (get_running_job_ids, mark_running_jobs_interrupted).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture(autouse=True)
def _reset_cron_running_set():
    import cron.scheduler as sched

    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()
    yield
    sched._running_job_ids.clear()
    sched._interrupted_job_ids.clear()


def _make_async_noop():
    async def _noop(*args, **kwargs):
        return None

    return _noop


class TestActiveCronJobCount:
    def test_zero_when_no_cron_jobs_running(self):
        runner, _adapter = make_restart_runner()
        assert runner._active_cron_job_count() == 0

    def test_reflects_cron_scheduler_state(self):
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        sched._running_job_ids.add("job-1")

        assert runner._active_cron_job_count() == 1

    def test_never_raises_if_cron_module_unavailable(self):
        """Best-effort: a broken/absent import must not take shutdown
        counting down with it."""
        runner, _adapter = make_restart_runner()

        with patch(
            "cron.scheduler.get_running_job_ids", side_effect=ImportError("boom")
        ):
            assert runner._active_cron_job_count() == 0


class TestGrantCronShutdownGrace:
    """Keepalive hardening: in-flight cron jobs get a bounded window to
    finish BEFORE the global tool-subprocess sweep amputates their subprocess
    (general shutdown failure mode reported for the T24-verdict one-shot).
    The grace is one-shot per shutdown and no-ops when no cron job is in
    flight or the grace is 0.
    """

    @pytest.mark.asyncio
    async def test_noop_when_no_cron_jobs_in_flight(self, monkeypatch):
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        runner._load_cron_shutdown_grace = lambda: 30.0
        sched._running_job_ids.clear()

        # Should return immediately without waiting (nothing in flight).
        await asyncio.wait_for(runner._grant_cron_shutdown_grace(), timeout=1.0)
        assert getattr(runner, "_cron_grace_granted", False) is False

    @pytest.mark.asyncio
    async def test_noop_when_grace_disabled(self, monkeypatch):
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        runner._load_cron_shutdown_grace = lambda: 0.0
        sched._running_job_ids.add("job-1")

        await asyncio.wait_for(runner._grant_cron_shutdown_grace(), timeout=1.0)
        # Flag stays unset: a zero grace must not consume the one-shot guard.
        assert getattr(runner, "_cron_grace_granted", False) is False

    @pytest.mark.asyncio
    async def test_waits_for_in_flight_cron_job_to_finish(self):
        """A cron job that clears within the grace must be allowed to finish
        naturally (its tool subprocess survives) instead of being swept."""
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        runner._load_cron_shutdown_grace = lambda: 5.0
        sched._running_job_ids.add("job-1")

        async def finish_job():
            await asyncio.sleep(0.15)
            sched._running_job_ids.discard("job-1")

        task = asyncio.create_task(finish_job())
        # Completes promptly (well under the 5s grace) once the job clears.
        await asyncio.wait_for(runner._grant_cron_shutdown_grace(), timeout=2.0)
        await task

        assert "job-1" not in sched._running_job_ids
        # One-shot guard consumed so the sweep path doesn't wait again.
        assert getattr(runner, "_cron_grace_granted", False) is True

    @pytest.mark.asyncio
    async def test_returns_after_grace_when_job_never_finishes(self):
        """A job that outlives the grace must NOT be waited on forever — the
        bounded fallback (sweep + interrupted-marking) still runs for it."""
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        runner._load_cron_shutdown_grace = lambda: 0.2
        sched._running_job_ids.add("job-1")

        start = asyncio.get_running_loop().time()
        await runner._grant_cron_shutdown_grace()
        elapsed = asyncio.get_running_loop().time() - start

        # Bounded by the grace, not forever.
        assert elapsed >= 0.15
        assert elapsed < 5.0
        assert "job-1" in sched._running_job_ids  # still in flight for the fallback

    @pytest.mark.asyncio
    async def test_one_shot_guard_skips_second_wait(self):
        """The grace is granted at most once per shutdown; a second call (e.g.
        the final-cleanup sweep after a post-interrupt sweep) must not wait
        again."""
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        runner._load_cron_shutdown_grace = lambda: 0.2
        sched._running_job_ids.add("job-1")

        start = asyncio.get_running_loop().time()
        await runner._grant_cron_shutdown_grace()
        first_elapsed = asyncio.get_running_loop().time() - start

        # Second call returns immediately (guard consumed).
        start2 = asyncio.get_running_loop().time()
        await asyncio.wait_for(runner._grant_cron_shutdown_grace(), timeout=0.5)
        second_elapsed = asyncio.get_running_loop().time() - start2

        assert first_elapsed >= 0.15
        assert second_elapsed < 0.15


class TestDrainWaitsForCronWork:
    @pytest.mark.asyncio
    async def test_drain_returns_immediately_when_nothing_active(self):
        runner, _adapter = make_restart_runner()

        _snapshot, timed_out = await runner._drain_active_agents(5.0)

        assert timed_out is False

    @pytest.mark.asyncio
    async def test_drain_waits_for_in_flight_cron_job(self):
        """Before this fix, a cron-only workload made active_at_start=0
        and the drain returned instantly -- this is the exact repro from
        the issue (a `sleep 1800` cron job in flight during /update)."""
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        sched._running_job_ids.add("job-1")

        async def finish_job():
            await asyncio.sleep(0.12)
            sched._running_job_ids.discard("job-1")

        task = asyncio.create_task(finish_job())
        _snapshot, timed_out = await runner._drain_active_agents(2.0)
        await task

        assert timed_out is False, (
            "drain must wait for the cron job to finish, not report "
            "active_at_start=0 and return instantly"
        )

    @pytest.mark.asyncio
    async def test_drain_times_out_if_cron_job_outlives_the_window(self):
        import cron.scheduler as sched

        runner, _adapter = make_restart_runner()
        sched._running_job_ids.add("job-1")  # never removed within the window

        _snapshot, timed_out = await runner._drain_active_agents(0.1)

        assert timed_out is True

    @pytest.mark.asyncio
    async def test_drain_still_waits_for_chat_sessions_unchanged(self):
        """Regression guard: folding cron into the check must not break
        the pre-existing chat-session drain behavior."""
        runner, _adapter = make_restart_runner()
        runner._running_agents = {"session-1": MagicMock()}

        async def finish_agent():
            await asyncio.sleep(0.12)
            runner._running_agents.clear()

        task = asyncio.create_task(finish_agent())
        _snapshot, timed_out = await runner._drain_active_agents(2.0)
        await task

        assert timed_out is False


class TestKillToolSubprocessesMarksCronInterrupted:
    @pytest.mark.asyncio
    async def test_in_flight_cron_job_marked_interrupted_on_forced_kill(self, monkeypatch):
        import cron.scheduler as sched
        import tools.process_registry as _pr
        import tools.terminal_tool as _tt
        import tools.browser_tool as _bt

        runner, adapter = make_restart_runner()
        runner._restart_drain_timeout = 0.01  # force the timeout path
        adapter.disconnect = _make_async_noop()

        sched._running_job_ids.add("job-1")

        monkeypatch.setattr(_pr.process_registry, "kill_all", lambda task_id=None: 1)
        monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
        monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)

        marked_calls = []
        real_mark = sched.mark_running_jobs_interrupted

        def _spy(reason):
            result = real_mark(reason)
            marked_calls.append((reason, result))
            return result

        monkeypatch.setattr(sched, "mark_running_jobs_interrupted", _spy)

        with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"), \
             patch("cron.scheduler.mark_job_run"):
            await runner.stop()

        assert marked_calls, "mark_running_jobs_interrupted was never called during shutdown"
        assert any(result == ["job-1"] for _reason, result in marked_calls)

    @pytest.mark.asyncio
    async def test_no_cron_jobs_running_is_a_silent_no_op(self, monkeypatch):
        """Graceful shutdown with nothing in flight must not spuriously
        mark or log anything cron-related."""
        import tools.process_registry as _pr
        import tools.terminal_tool as _tt
        import tools.browser_tool as _bt

        runner, adapter = make_restart_runner()
        adapter.disconnect = _make_async_noop()

        monkeypatch.setattr(_pr.process_registry, "kill_all", lambda task_id=None: 0)
        monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
        monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)

        with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"), \
             patch("cron.scheduler.mark_job_run") as mock_mark:
            await runner.stop()

        mock_mark.assert_not_called()


class TestCronShutdownGracePreservesSubprocess:
    """Integration: the grace window must actually prevent the global
    tool-subprocess sweep from amputating an in-flight cron job's
    subprocess when that job clears (and its tool subprocess exits)
    within the grace.

    End-to-end guarantee the T24-verdict regression exercised: a
    long-running cron tool subprocess that finishes during the grace must
    survive ``_kill_tool_subprocesses("final-cleanup")`` instead of being
    reaped with an interrupted mark (#60432 amputation).
    """

    @pytest.mark.asyncio
    async def test_subprocess_survives_final_cleanup_via_grace(self, monkeypatch):
        import sys
        import cron.scheduler as sched
        import tools.process_registry as _pr

        runner = make_restart_runner()[0]
        runner._load_cron_shutdown_grace = lambda: 5.0

        # Spawn a REAL short-lived subprocess tracked by the process
        # registry, so a global kill_all sweep would actually reap it if
        # the grace did not run first.
        session = _pr.process_registry.spawn_local(
            command=f"{sys.executable!r} -c 'import time; time.sleep(0.3)'",
            task_id="cron-verdict",
        )
        assert session is not None
        sched._running_job_ids.add("cron-verdict")

        async def _release_after_subprocess_exits():
            # Mirror the real cron completion path: the scheduler drops the
            # in-flight job id once run_one_job() returns (which happens
            # after the tool subprocess it owns exits).
            await asyncio.to_thread(session.process.wait, 5)
            sched._running_job_ids.discard("cron-verdict")

        releaser = asyncio.create_task(_release_after_subprocess_exits())
        try:
            # The grace must WAIT for the cron job to clear rather than
            # returning immediately, so the subprocess gets to finish.
            await asyncio.wait_for(runner._grant_cron_shutdown_grace(), timeout=5.0)
            await releaser

            assert "cron-verdict" not in sched._running_job_ids
            # Final-cleanup sweep now finds nothing to reap — the job's
            # tool subprocess exited on its own, surviving the shutdown.
            assert _pr.process_registry.kill_all() == 0
        finally:
            _pr.process_registry.kill_all()

