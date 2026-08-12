"""Tests for cronjob action='run' background dispatch.

A manual `cronjob(action='run')` used to execute the job synchronously on the
calling agent's tool thread — a full agent run (minutes to hours) inside ONE
tool call, uninterruptible and serial. It now dispatches through the async
delegation registry (same rail as delegate_task background mode): the tool
returns immediately with a handle and the run's outcome re-enters the
conversation as a type='async_delegation' completion event.

Sync fallbacks preserved:
  - no routable session (direct Python callers, `hermes cron run`)
  - async delivery unsupported (one-shot runners, cron child sessions)
  - dispatch pool at capacity (claim already taken — must not strand it)
"""
import json
import threading
import time
from unittest.mock import patch

from tools.cronjob_tools import (
    _try_dispatch_background_run,
    cronjob,
)


_JOB = {"id": "job-bg-1", "name": "bg run", "prompt": "hi",
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


def _job(job_id):
    """Per-test job dict with a UNIQUE id.

    Background workers outlive their test (daemon executor) and hold the id
    in the scheduler's shared running set until the run finishes; reusing one
    id across tests makes the in-flight dedupe guard see a phantom
    'already running' from a previous test's straggler worker.
    """
    return {"id": job_id, "name": f"bg run {job_id}", "prompt": "hi",
            "schedule": {"kind": "cron", "expr": "0 9 * * *"}}


_ATTEMPT_OWNER = "manual-attempt-token"


def _claimed_job(
    job_id,
    *,
    status="ok",
    error=None,
    next_run_at=None,
    owner=_ATTEMPT_OWNER,
):
    job = {
        **_job(job_id),
        "last_status": status,
        "last_error": error,
        "run_claim": {
            "at": "2026-08-11T00:00:00+00:00",
            "by": owner,
        },
    }
    if next_run_at is not None:
        job["next_run_at"] = next_run_at
    return job


def _bound_session_key(key="agent:main:telegram:dm:123"):
    """Context manager binding the approval session key contextvar."""
    import contextlib

    from tools.approval import _approval_session_key

    @contextlib.contextmanager
    def _cm():
        token = _approval_session_key.set(key)
        try:
            yield
        finally:
            _approval_session_key.reset(token)

    return _cm()


class TestBackgroundDispatch:
    def test_queued_cancel_terminalizes_exact_attempt_before_worker_starts(self):
        """A session reset can cancel the claim-to-worker registration gap."""
        captured = {}

        def capture_dispatch(**kwargs):
            captured.update(kwargs)
            return {"status": "dispatched", "delegation_id": "queued-cancel"}

        with _bound_session_key(), patch(
            "tools.cronjob_tools.claim_job_for_fire_attempt",
            return_value=_ATTEMPT_OWNER,
        ), patch(
            "tools.async_delegation.dispatch_async_delegation",
            side_effect=capture_dispatch,
        ), patch("tools.cronjob_tools.mark_job_run", return_value=True) as m_mark, \
             patch("tools.cronjob_tools.get_job") as m_get, \
             patch("cron.scheduler.run_one_job") as m_run:
            dispatched = _try_dispatch_background_run(_job("job-bg-queued-cancel"))
            captured["interrupt_fn"]()
            completed = captured["runner"]()

        assert dispatched["dispatched"] is True
        assert completed["status"] == "interrupted"
        assert "interrupted" in completed["error"].lower()
        m_get.assert_not_called()
        m_run.assert_not_called()
        m_mark.assert_called_once_with(
            "job-bg-queued-cancel",
            False,
            "Manual cron run interrupted because its owning session ended.",
            status="interrupted",
            expected_run_claim_owner=_ATTEMPT_OWNER,
        )

    def test_interrupt_after_runner_completion_is_noop(self):
        """A stale interrupt callback cannot rewrite a completed attempt."""
        captured = {}

        def capture_dispatch(**kwargs):
            captured.update(kwargs)
            return {"status": "dispatched", "delegation_id": "done-noop"}

        with _bound_session_key(), patch(
            "tools.cronjob_tools.claim_job_for_fire_attempt",
            return_value=_ATTEMPT_OWNER,
        ), patch(
            "tools.async_delegation.dispatch_async_delegation",
            side_effect=capture_dispatch,
        ), patch(
            "tools.cronjob_tools.get_job",
            return_value=_claimed_job("job-bg-done-noop"),
        ), patch("cron.scheduler.run_one_job", return_value=True), patch(
            "tools.cronjob_tools.mark_job_run"
        ) as m_mark:
            _try_dispatch_background_run(_job("job-bg-done-noop"))
            completed = captured["runner"]()
            captured["interrupt_fn"]()

        assert completed["status"] == "completed"
        m_mark.assert_not_called()

    def test_interrupt_callback_is_pinned_to_dispatch_profile(self, tmp_path):
        """The session/shutdown thread cannot cancel a same-id peer profile."""
        import cron.jobs as jobs
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        job_id = "job-bg-profile-cancel"
        default_home = tmp_path / "default"
        profile_home = tmp_path / "profiles" / "ops"
        captured = {}

        def capture_dispatch(**kwargs):
            captured.update(kwargs)
            return {"status": "dispatched", "delegation_id": "profile-cancel"}

        default_token = set_hermes_home_override(default_home)
        try:
            jobs.save_jobs([
                _claimed_job(job_id, owner="successor-in-default-profile")
            ])
        finally:
            reset_hermes_home_override(default_token)

        profile_token = set_hermes_home_override(profile_home)
        try:
            jobs.save_jobs([_claimed_job(job_id)])
            with _bound_session_key(), patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), patch(
                "tools.async_delegation.dispatch_async_delegation",
                side_effect=capture_dispatch,
            ):
                _try_dispatch_background_run(_job(job_id))
        finally:
            reset_hermes_home_override(profile_token)

        # Execute from a context explicitly pointing at the OTHER profile,
        # matching /new and gateway-shutdown callback threads.
        default_token = set_hermes_home_override(default_home)
        try:
            captured["interrupt_fn"]()
            default_job = jobs.get_job(job_id)
        finally:
            reset_hermes_home_override(default_token)

        profile_token = set_hermes_home_override(profile_home)
        try:
            profile_job = jobs.get_job(job_id)
        finally:
            reset_hermes_home_override(profile_token)

        assert default_job["run_claim"]["by"] == "successor-in-default-profile"
        assert default_job["last_status"] == "ok"
        assert profile_job["run_claim"] is None
        assert profile_job["last_status"] == "interrupted"

    def test_interrupt_for_session_stops_running_manual_cron(self):
        """``/new`` interrupts a running manual cron through the shared event."""
        from tools.async_delegation import interrupt_for_session

        run_started = threading.Event()
        run_stopped = threading.Event()
        seen_cancel_event = {}

        def blocking_run(_job, *, _attempt_outcome=None, cancel_event=None, **_kw):
            seen_cancel_event["event"] = cancel_event
            run_started.set()
            assert cancel_event is not None
            assert cancel_event.wait(timeout=5.0)
            _attempt_outcome.update(
                success=False,
                error="Manual cron run interrupted because its owning session ended.",
                output_file=None,
                terminal_recorded=False,
            )
            run_stopped.set()
            return False

        session_key = "agent:main:telegram:dm:cron-cancel-session"
        with _bound_session_key(session_key), patch(
            "tools.cronjob_tools.claim_job_for_fire_attempt",
            return_value=_ATTEMPT_OWNER,
        ), patch(
            "tools.cronjob_tools.get_job",
            return_value=_claimed_job("job-bg-session-cancel"),
        ), patch(
            "cron.scheduler.run_one_job", side_effect=blocking_run
        ), patch("tools.cronjob_tools.mark_job_run", return_value=True) as m_mark:
            dispatched = _try_dispatch_background_run(
                _job("job-bg-session-cancel"), session_id="parent-session-1"
            )
            assert run_started.wait(timeout=5.0)
            assert interrupt_for_session(session_key=session_key) == 1
            assert run_stopped.wait(timeout=5.0)

            from tools.process_registry import process_registry

            found = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    event = process_registry.completion_queue.get_nowait()
                except Exception:
                    time.sleep(0.01)
                    continue
                if event.get("delegation_id") == dispatched["delegation_id"]:
                    found = event
                    break
                process_registry.completion_queue.put(event)
                time.sleep(0.01)

        assert seen_cancel_event["event"].is_set()
        assert found is not None
        assert found["status"] == "interrupted"
        assert "interrupted" in (found.get("error") or "").lower()
        m_mark.assert_called_once_with(
            "job-bg-session-cancel",
            False,
            "Manual cron run interrupted because its owning session ended.",
            status="interrupted",
            expected_run_claim_owner=_ATTEMPT_OWNER,
        )

    def test_interrupt_all_stops_running_manual_cron(self):
        """Gateway shutdown interrupts manual cron, not only subagents."""
        from tools.async_delegation import interrupt_all

        run_started = threading.Event()
        run_stopped = threading.Event()

        def blocking_run(_job, *, _attempt_outcome=None, cancel_event=None, **_kw):
            run_started.set()
            assert cancel_event is not None
            assert cancel_event.wait(timeout=5.0)
            _attempt_outcome.update(
                success=False,
                error="Manual cron run interrupted because its owning session ended.",
                output_file=None,
                terminal_recorded=False,
            )
            run_stopped.set()
            return False

        with _bound_session_key("agent:main:telegram:dm:cron-shutdown"), patch(
            "tools.cronjob_tools.claim_job_for_fire_attempt",
            return_value=_ATTEMPT_OWNER,
        ), patch(
            "tools.cronjob_tools.get_job",
            return_value=_claimed_job("job-bg-shutdown-cancel"),
        ), patch(
            "cron.scheduler.run_one_job", side_effect=blocking_run
        ), patch("tools.cronjob_tools.mark_job_run", return_value=True):
            _try_dispatch_background_run(_job("job-bg-shutdown-cancel"))
            assert run_started.wait(timeout=5.0)
            assert interrupt_all(reason="test shutdown") >= 1
            assert run_stopped.wait(timeout=5.0)

    def test_running_script_is_killed_without_late_side_effect(self, tmp_path):
        """The shared cancel event terminates the real script process group."""
        import cron.jobs as jobs
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from tools.async_delegation import interrupt_for_session
        from tools.process_registry import process_registry

        job_id = "job-bg-real-script-cancel"
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        started_file = tmp_path / "script-started"
        forbidden_file = tmp_path / "script-completed"
        (scripts_dir / "slow.py").write_text(
            "import pathlib, time\n"
            f"pathlib.Path({str(started_file)!r}).write_text('started')\n"
            "time.sleep(10)\n"
            f"pathlib.Path({str(forbidden_file)!r}).write_text('completed')\n",
            encoding="utf-8",
        )
        claimed = _claimed_job(job_id, status=None)
        claimed.update(
            no_agent=True,
            script="slow.py",
            deliver="local",
            schedule={"kind": "interval", "minutes": 60},
        )

        home_token = set_hermes_home_override(tmp_path)
        try:
            jobs.save_jobs([claimed])
            session_key = "agent:main:telegram:dm:cron-real-script"
            with _bound_session_key(session_key), patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ):
                dispatched = _try_dispatch_background_run(_job(job_id))
                deadline = time.monotonic() + 5.0
                while not started_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert started_file.exists(), "script process never started"
                assert interrupt_for_session(session_key=session_key) == 1

                completion = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        event = process_registry.completion_queue.get_nowait()
                    except Exception:
                        time.sleep(0.02)
                        continue
                    if event.get("delegation_id") == dispatched["delegation_id"]:
                        completion = event
                        break
                    process_registry.completion_queue.put(event)
                    time.sleep(0.02)
            persisted = jobs.get_job(job_id)
        finally:
            reset_hermes_home_override(home_token)

        # Give an accidentally orphaned child enough time to expose itself;
        # the correct path terminates it before the ten-second sleep completes.
        time.sleep(0.2)
        assert completion is not None
        assert completion["status"] == "interrupted"
        assert persisted["run_claim"] is None
        assert persisted["last_status"] == "interrupted"
        assert not forbidden_file.exists()

    def test_dispatches_and_returns_handle_immediately(self):
        """With a routable session, run claims sync then dispatches async."""
        run_started = threading.Event()
        run_release = threading.Event()

        def slow_run_one_job(job, **kw):
            run_started.set()
            assert run_release.wait(timeout=5.0)
            return True

        with _bound_session_key():
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ) as m_claim, \
                 patch("cron.scheduler.run_one_job", side_effect=slow_run_one_job), \
                 patch("tools.cronjob_tools.get_job",
                       return_value=_claimed_job("job-bg-01")):
                res = _try_dispatch_background_run(_job('job-bg-01'))
                try:
                    # Returned BEFORE the job finished — that's the whole point.
                    assert res is not None
                    assert res["claimed"] is True
                    assert res["dispatched"] is True
                    assert res["delegation_id"]
                    m_claim.assert_called_once_with("job-bg-01")
                    # Keep patches alive until the daemon worker enters the run.
                    assert run_started.wait(timeout=5.0), \
                        "job never started in background"
                finally:
                    run_release.set()

    def test_completion_event_reaches_shared_queue(self):
        """The finished run pushes a type='async_delegation' event carrying
        the job outcome onto process_registry.completion_queue."""
        import time

        from tools.process_registry import process_registry

        # The runner executes on a daemon thread — the patches must stay
        # active until the completion event lands, so poll INSIDE the blocks.
        with _bound_session_key("agent:main:telegram:dm:777"):
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), \
                 patch("cron.scheduler.run_one_job", return_value=True), \
                 patch("tools.cronjob_tools.get_job",
                       return_value=_claimed_job(
                           "job-bg-02",
                           next_run_at="2026-08-07T09:00:00",
                       )):
                res = _try_dispatch_background_run(_job('job-bg-02'))
                assert res["dispatched"] is True

                found = None
                for _ in range(100):
                    try:
                        evt = process_registry.completion_queue.get_nowait()
                    except Exception:
                        time.sleep(0.05)
                        continue
                    if (evt.get("type") == "async_delegation"
                            and evt.get("delegation_id") == res["delegation_id"]):
                        found = evt
                        break
                    process_registry.completion_queue.put(evt)
                    time.sleep(0.05)
        assert found is not None, "completion event never reached the queue"
        assert found["session_key"] == "agent:main:telegram:dm:777"
        assert found["status"] == "completed"
        assert "bg run" in (found.get("summary") or "")
        assert "Next scheduled run" in found["summary"]

    def test_failed_run_reports_error_status_in_event(self):
        import time

        from tools.process_registry import process_registry

        def failed_attempt(_job, *, _attempt_outcome=None, **_kwargs):
            _attempt_outcome.update(
                success=False,
                error="provider exploded",
                output_file=None,
                terminal_recorded=True,
            )
            return True

        with _bound_session_key("agent:main:telegram:dm:778"):
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), \
                 patch("cron.scheduler.run_one_job", side_effect=failed_attempt), \
                 patch("tools.cronjob_tools._attempt_output_excerpt") as excerpt, \
                 patch("tools.cronjob_tools.get_job",
                       return_value=_claimed_job(
                           "job-bg-03",
                           status="error",
                           error="provider exploded",
                       )):
                res = _try_dispatch_background_run(_job('job-bg-03'))
                assert res["dispatched"] is True

                found = None
                for _ in range(100):
                    try:
                        evt = process_registry.completion_queue.get_nowait()
                    except Exception:
                        time.sleep(0.05)
                        continue
                    if evt.get("delegation_id") == res["delegation_id"]:
                        found = evt
                        break
                    process_registry.completion_queue.put(evt)
                    time.sleep(0.05)
        assert found is not None
        assert found["status"] == "error"
        assert "provider exploded" in (found.get("error") or "")
        excerpt.assert_not_called()

    def test_claim_lost_reports_immediately_without_dispatch(self):
        """Paused/already-firing jobs report in the tool response, not as a
        delayed completion event."""
        with _bound_session_key():
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=None,
            ), \
                 patch("tools.cronjob_tools.get_job",
                       return_value={**_JOB, "enabled": False}), \
                 patch("tools.async_delegation.dispatch_async_delegation") as m_disp:
                res = _try_dispatch_background_run(_job('job-bg-04'))
        assert res["claimed"] is False
        assert "paused/disabled" in res["error"]
        m_disp.assert_not_called()

    def test_dispatched_runner_rejects_successor_attempt(self):
        """A queued background runner cannot adopt a replacement token."""
        captured = {}
        replacement = _claimed_job(
            "job-bg-successor",
            owner="successor-attempt-token",
        )

        def capture_runner(**kwargs):
            captured["runner"] = kwargs["runner"]
            return {"status": "dispatched", "delegation_id": "delegation-1"}

        with _bound_session_key():
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), patch(
                "tools.async_delegation.dispatch_async_delegation",
                side_effect=capture_runner,
            ), patch(
                "tools.cronjob_tools.get_job",
                return_value=replacement,
            ), patch("cron.scheduler.run_one_job") as m_run, patch(
                "tools.cronjob_tools.mark_job_run"
            ) as m_mark:
                dispatched = _try_dispatch_background_run(
                    _job("job-bg-successor")
                )
                completed = captured["runner"]()

        assert dispatched["dispatched"] is True
        assert completed["status"] == "error"
        assert "claim could not be reloaded safely" in completed["error"]
        m_run.assert_not_called()
        m_mark.assert_called_once_with(
            "job-bg-successor",
            False,
            "Job attempt claim could not be reloaded safely; not executing",
            expected_run_claim_owner=_ATTEMPT_OWNER,
        )

    def test_background_worker_keeps_dispatching_profile_context(self, tmp_path):
        """The daemon worker must resolve the same profile that claimed it."""
        from hermes_constants import (
            get_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        profile_home = (tmp_path / "profiles" / "ops").resolve()
        run_started = threading.Event()
        observed = {}

        def probe_profile(job, **kwargs):
            observed["home"] = get_hermes_home().resolve()
            run_started.set()
            return True

        token = set_hermes_home_override(profile_home)
        try:
            with _bound_session_key(), patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), patch(
                "cron.scheduler.run_one_job", side_effect=probe_profile
            ), patch(
                "tools.cronjob_tools.get_job",
                return_value=_claimed_job("job-bg-profile"),
            ):
                res = _try_dispatch_background_run(_job("job-bg-profile"))
                assert res["dispatched"] is True
                assert run_started.wait(timeout=5.0), \
                    "background run never started"
        finally:
            reset_hermes_home_override(token)

        assert observed["home"] == profile_home


class TestSyncFallbacks:
    def test_no_session_key_falls_back_to_sync(self):
        """Direct Python callers (no agent session) keep the sync path."""
        res = _try_dispatch_background_run(_job('job-bg-05'))
        assert res is None

    def test_async_delivery_unsupported_falls_back_to_sync(self):
        """One-shot runtimes (hermes -z, cron child, Kanban) keep sync."""
        with _bound_session_key():
            with patch("gateway.session_context.async_delivery_supported",
                       return_value=False):
                res = _try_dispatch_background_run(_job('job-bg-06'))
        assert res is None

    def test_pool_at_capacity_runs_inline(self):
        """A rejected dispatch must not strand the already-taken claim."""
        with _bound_session_key():
            with patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), \
                 patch("tools.async_delegation.dispatch_async_delegation",
                       return_value={"status": "rejected", "error": "capacity"}), \
                 patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
                 patch("tools.cronjob_tools.get_job",
                       return_value=_claimed_job("job-bg-07")):
                res = _try_dispatch_background_run(_job('job-bg-07'))
        assert res["dispatched"] is False
        assert res["success"] is True
        m_run.assert_called_once()   # ran inline on this thread

    def test_dispatch_exception_runs_inline_without_stranding_claim(self, tmp_path):
        """A registry exception has the same safe fallback as rejection."""
        import cron.jobs as jobs

        job_id = "job-bg-dispatch-raise"
        claimed = _claimed_job(job_id, status=None)
        claimed["schedule"] = {"kind": "interval", "minutes": 60}

        def complete_run(job, **kwargs):
            return jobs.mark_job_run(
                job_id,
                True,
                expected_run_claim_owner=_ATTEMPT_OWNER,
            )

        with jobs.use_cron_store(tmp_path):
            jobs.save_jobs([claimed])
            with _bound_session_key(), patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value=_ATTEMPT_OWNER,
            ), patch(
                "tools.async_delegation.dispatch_async_delegation",
                side_effect=RuntimeError("registry unavailable"),
            ), patch(
                "cron.scheduler.run_one_job", side_effect=complete_run
            ) as m_run:
                res = _try_dispatch_background_run(_job(job_id))
            persisted = jobs.get_job(job_id)

        assert res["claimed"] is True
        assert res["dispatched"] is False
        assert res["success"] is True
        m_run.assert_called_once()
        assert persisted["run_claim"] is None
        assert persisted["last_status"] == "ok"


class TestInFlightDedupe:
    """Manual runs must not double-fire a job that is already mid-run
    (salvaged from #53395 by @izumi0uu): the fire claim's 300s TTL is
    routinely outlived by real jobs, so the claim alone can't prevent it."""

    def test_run_claimed_job_skips_when_already_running(self):
        """The authoritative guard: _run_claimed_job refuses to fire a job
        whose id is already registered in the scheduler running set."""
        from cron import scheduler as sched
        from tools.cronjob_tools import _run_claimed_job

        assert sched.try_register_running_job("job-bg-08")   # simulate ticker mid-run
        try:
            with patch("cron.scheduler.run_one_job") as m_run, patch(
                "tools.cronjob_tools.release_run_claim"
            ) as m_release:
                res = _run_claimed_job(
                    _job('job-bg-08'),
                    attempt_owner=_ATTEMPT_OWNER,
                )
            assert res["success"] is False
            assert "already running" in res["error"]
            m_run.assert_not_called()
            m_release.assert_called_once_with(
                "job-bg-08",
                expected_owner=_ATTEMPT_OWNER,
            )
        finally:
            sched.release_running_job("job-bg-08")

    def test_run_claimed_job_registers_and_releases(self):
        """A normal run holds the registration for run_one_job's duration and
        releases it after — visible to get_running_job_ids mid-run."""
        from cron import scheduler as sched
        from tools.cronjob_tools import _run_claimed_job

        seen_during_run = {}

        def probe_run(job, **kw):
            seen_during_run["registered"] = "job-bg-09" in sched.get_running_job_ids()
            return True

        with patch("cron.scheduler.run_one_job", side_effect=probe_run), \
             patch("tools.cronjob_tools.get_job",
                   return_value=_claimed_job("job-bg-09")):
            res = _run_claimed_job(
                _job('job-bg-09'),
                attempt_owner=_ATTEMPT_OWNER,
            )

        assert res["success"] is True
        assert seen_during_run["registered"] is True
        assert "job-bg-09" not in sched.get_running_job_ids()   # released after

    def test_background_dispatch_reports_running_job_immediately(self):
        """The dispatch path pre-checks the running set so a mid-run job
        reports in the tool response, not as a delayed completion event."""
        from cron import scheduler as sched

        assert sched.try_register_running_job("job-bg-10")
        try:
            with _bound_session_key():
                with patch(
                    "tools.cronjob_tools.claim_job_for_fire_attempt"
                ) as m_claim, \
                     patch("tools.async_delegation.dispatch_async_delegation") as m_disp:
                    res = _try_dispatch_background_run(_job('job-bg-10'))
            assert res["claimed"] is False
            assert "already running" in res["error"]
            m_claim.assert_not_called()   # no claim consumed for a skipped run
            m_disp.assert_not_called()
        finally:
            sched.release_running_job("job-bg-10")

    def test_ticker_guard_uses_shared_helpers(self):
        """The ticker's _submit_with_guard and manual runs share ONE dedupe
        owner: registration through either side blocks the other."""
        from cron import scheduler as sched

        # Manual-run registration…
        assert sched.try_register_running_job("job-shared-1")
        try:
            # …is exactly what the ticker-side helper consults.
            assert not sched.try_register_running_job("job-shared-1")
            assert "job-shared-1" in sched.get_running_job_ids()
        finally:
            sched.release_running_job("job-shared-1")
        assert "job-shared-1" not in sched.get_running_job_ids()
        # Idempotent release: never raises on a non-member.
        sched.release_running_job("job-shared-1")

    def test_same_job_id_in_two_profiles_registers_independently(self, tmp_path):
        from cron import scheduler as sched
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        profiles = [tmp_path / "default", tmp_path / "profiles" / "ops"]
        registrations = []
        for index, profile_home in enumerate(profiles):
            token = set_hermes_home_override(profile_home)
            try:
                registrations.append(
                    sched.try_register_running_job(
                        "shared-job",
                        profile_scoped=True,
                        attempt_owner=f"attempt-{index}",
                    )
                )
            finally:
                reset_hermes_home_override(token)

        try:
            assert registrations == [True, True]
            # This helper intentionally exposes process-wide activity, including
            # daemon workers started by earlier background tests.  Only this
            # profile-pair's shared membership is stable here.
            assert "shared-job" in sched.get_running_job_ids()
        finally:
            for index, profile_home in enumerate(profiles):
                token = set_hermes_home_override(profile_home)
                try:
                    assert sched.release_running_job(
                        "shared-job",
                        profile_scoped=True,
                        attempt_owner=f"attempt-{index}",
                    )
                finally:
                    reset_hermes_home_override(token)

    def test_background_precheck_is_profile_aware(self, tmp_path):
        from cron import scheduler as sched
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        profile_a = tmp_path / "profiles" / "a"
        profile_b = tmp_path / "profiles" / "b"
        token_a = set_hermes_home_override(profile_a)
        try:
            assert sched.try_register_running_job(
                "same-id",
                profile_scoped=True,
                attempt_owner="profile-a-attempt",
            )
        finally:
            reset_hermes_home_override(token_a)

        token_b = set_hermes_home_override(profile_b)
        try:
            with _bound_session_key(), patch(
                "tools.cronjob_tools.claim_job_for_fire_attempt",
                return_value="profile-b-attempt",
            ) as m_claim, patch(
                "tools.async_delegation.dispatch_async_delegation",
                return_value={
                    "status": "dispatched",
                    "delegation_id": "profile-b-delegation",
                },
            ):
                res = _try_dispatch_background_run(_job("same-id"))
        finally:
            reset_hermes_home_override(token_b)

        try:
            assert res["dispatched"] is True
            m_claim.assert_called_once_with("same-id")
        finally:
            token_a = set_hermes_home_override(profile_a)
            try:
                sched.release_running_job(
                    "same-id",
                    profile_scoped=True,
                    attempt_owner="profile-a-attempt",
                )
            finally:
                reset_hermes_home_override(token_a)


class TestCronjobRunToolIntegration:
    def test_run_action_returns_background_note(self):
        """cronjob(action='run') surfaces the handle + do-not-wait note."""
        with _bound_session_key():
            with patch("tools.cronjob_tools.resolve_job_ref", return_value=_job('job-bg-12')), \
                 patch(
                     "tools.cronjob_tools.claim_job_for_fire_attempt",
                     return_value=_ATTEMPT_OWNER,
                 ), \
                 patch("cron.scheduler.run_one_job", return_value=True), \
                 patch("tools.cronjob_tools.get_job",
                       return_value=_claimed_job("job-bg-12")):
                out = json.loads(cronjob(action="run", job_id="job-bg-12"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_mode"] == "background"
        assert out["job"]["delegation_id"]
        assert "background" in out["note"]

    def test_run_action_sync_path_unchanged_without_session(self):
        """No session context → the legacy synchronous behavior (executed +
        execution_success populated from the completed run)."""
        ran = _claimed_job("job-bg-13")
        with patch("tools.cronjob_tools.resolve_job_ref", return_value=_job('job-bg-13')), \
             patch(
                 "tools.cronjob_tools.claim_job_for_fire_attempt",
                 return_value=_ATTEMPT_OWNER,
             ) as m_claim, \
             patch("cron.scheduler.run_one_job", return_value=True) as m_run, \
             patch("tools.cronjob_tools.get_job", return_value=ran):
            out = json.loads(cronjob(action="run", job_id="job-bg-13"))

        assert out["success"] is True
        assert out["job"]["executed"] is True
        assert out["job"]["execution_success"] is True
        m_claim.assert_called_once_with("job-bg-13")
        m_run.assert_called_once()
