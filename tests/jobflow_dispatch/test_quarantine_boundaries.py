"""Acting boundaries participate in the durable quarantine dispatch fence."""

from __future__ import annotations

import contextlib

import pytest


class _FenceStore:
    def __init__(self, calls, *, refuse=False):
        self.calls = calls
        self.refuse = refuse

    @contextlib.contextmanager
    def dispatch_section(self, *, boundary):
        self.calls.append(("enter", boundary))
        if self.refuse:
            raise RuntimeError("dispatch fenced")
        try:
            yield
        finally:
            self.calls.append(("exit", boundary))


def test_tick_holds_section_from_before_due_capture_through_submit(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    store = _FenceStore(calls)
    monkeypatch.setattr(scheduler, "default_control_store", lambda: store, raising=False)
    monkeypatch.setattr(
        scheduler,
        "get_due_and_skipped_jobs",
        lambda: calls.append("capture") or ([{"id": "job-1", "name": "one"}], []),
    )
    monkeypatch.setattr(scheduler, "_collect_woken_jobs", lambda **_: [])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda _job_id: calls.append("advance"))
    monkeypatch.setattr(scheduler, "create_execution", lambda *_a, **_k: {"id": "exec-1"})

    class _Future:
        def result(self):
            return True

        def add_done_callback(self, callback):
            callback(self)

        def exception(self):
            return None

    class _Pool:
        def submit(self, _callable):
            calls.append("submit")
            return _Future()

    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _limit: _Pool())
    monkeypatch.setattr(scheduler, "_sweep_mcp_orphans", lambda: None, raising=False)

    assert scheduler.tick(verbose=False, sync=False) == 1
    assert calls.index(("enter", "cron-tick")) < calls.index("capture")
    assert calls.index("submit") < calls.index(("exit", "cron-tick"))


def test_sync_tick_releases_section_after_submit_before_waiting_for_result(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    store = _FenceStore(calls)
    monkeypatch.setattr(scheduler, "default_control_store", lambda: store, raising=False)
    monkeypatch.setattr(
        scheduler,
        "get_due_and_skipped_jobs",
        lambda: calls.append("capture") or ([{"id": "job-sync", "name": "one"}], []),
    )
    monkeypatch.setattr(scheduler, "_collect_woken_jobs", lambda **_: [])
    monkeypatch.setattr(scheduler, "advance_next_run", lambda _job_id: None)
    monkeypatch.setattr(scheduler, "create_execution", lambda *_a, **_k: {"id": "exec-1"})

    class _Future:
        def result(self):
            calls.append("wait")
            return True

    class _Pool:
        def submit(self, _callable):
            calls.append("submit")
            return _Future()

    monkeypatch.setattr(scheduler, "_get_parallel_pool", lambda _limit: _Pool())
    monkeypatch.setattr(
        scheduler.concurrent.futures,
        "as_completed",
        lambda futures: iter(futures),
    )

    assert scheduler.tick(verbose=False, sync=True) == 1
    assert calls.index("submit") < calls.index(("exit", "cron-tick"))
    assert calls.index(("exit", "cron-tick")) < calls.index("wait")


def test_tick_refuses_before_due_capture(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "default_control_store", lambda: _FenceStore(calls, refuse=True), raising=False
    )
    monkeypatch.setattr(
        scheduler,
        "get_due_and_skipped_jobs",
        lambda: pytest.fail("due rows captured after fence refusal"),
    )

    with pytest.raises(RuntimeError, match="dispatch fenced"):
        scheduler.tick(verbose=False, sync=False)


def test_provider_fire_holds_section_before_claim_through_running_handoff(monkeypatch):
    import cron.executions as executions
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    import cron.scheduler_provider as provider

    calls = []
    monkeypatch.setattr(provider, "default_control_store", lambda: _FenceStore(calls), raising=False)
    monkeypatch.setattr(jobs, "claim_job_for_fire", lambda _jid: calls.append("claim") or True)
    monkeypatch.setattr(jobs, "get_job", lambda jid: {"id": jid, "name": "one"})
    monkeypatch.setattr(executions, "create_execution", lambda *_a, **_k: {"id": "exec-1"})

    def handoff(*_args, **kwargs):
        calls.append("handoff")
        kwargs["_dispatch_admission"].__exit__(None, None, None)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", handoff)

    assert provider.InProcessCronScheduler().fire_due("job-1") is True
    assert calls.index(("enter", "external-provider-fire")) < calls.index("claim")
    assert calls.index("handoff") < calls.index(("exit", "external-provider-fire"))


def test_direct_run_releases_admission_after_running_before_work(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "default_control_store", lambda: _FenceStore(calls), raising=False
    )
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_a, **_k: calls.append("create") or {"id": "exec-1"},
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda *_a: True)
    monkeypatch.setattr(
        scheduler,
        "mark_execution_running",
        lambda *_a: calls.append("running"),
    )
    monkeypatch.setattr(
        scheduler,
        "_get_event_emitter",
        lambda: None,
    )
    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_a, **_k: calls.append("work") or (True, "", "", None),
    )
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_a, **_k: None)

    assert scheduler.run_one_job({"id": "job-1", "name": "one", "no_agent": True})
    assert calls.index("running") < calls.index(("exit", "direct-run"))
    assert calls.index(("exit", "direct-run")) < calls.index("work")


def test_direct_run_refuses_before_execution_creation(monkeypatch):
    import cron.scheduler as scheduler

    calls = []
    monkeypatch.setattr(
        scheduler, "default_control_store", lambda: _FenceStore(calls, refuse=True), raising=False
    )
    monkeypatch.setattr(
        scheduler,
        "create_execution",
        lambda *_a, **_k: pytest.fail("execution created after fence refusal"),
    )

    with pytest.raises(RuntimeError, match="dispatch fenced"):
        scheduler.run_one_job({"id": "job-1", "name": "one"})


def test_reconciler_holds_section_across_resolve_through_request(monkeypatch):
    import jobflow_dispatch.activate as activate

    calls = []
    monkeypatch.setattr(activate, "default_control_store", lambda: _FenceStore(calls), raising=False)
    report = activate.activate_pending(
        [activate.Activation("a.one", "main", "key", None, "reconcile")],
        resolve=lambda _activity: calls.append("resolve") or "job-1",
        request_run=lambda *_a, **_k: calls.append("request") or {"id": "job-1"},
    )

    assert report.activated == ("job-1",)
    assert calls.index(("enter", "jobflow-reconciler")) < calls.index("resolve")
    assert calls.index("request") < calls.index(("exit", "jobflow-reconciler"))


def test_dispatcher_refuses_before_claim(monkeypatch):
    from events.schema import Event, EventType, Priority
    from events.subscribers.jobflow_dispatcher import JobFlowDispatcher
    import events.subscribers.jobflow_dispatcher as dispatcher_module

    calls = []
    monkeypatch.setattr(
        dispatcher_module,
        "default_control_store",
        lambda: _FenceStore(calls, refuse=True),
        raising=False,
    )

    class _Store:
        lease_seconds = 10

        def claim_for_wake(self, *_a, **_k):
            pytest.fail("activation claimed after fence refusal")

    event = Event(
        event_id="event-1",
        event_type=EventType.MAILBOX_MESSAGE,
        timestamp="2026-08-21T00:00:00Z",
        source="test",
        priority=Priority.NORMAL,
        payload={"message_type": "SCORE_REQUEST", "to": "matcher", "file": "matcher/inbox/m1.json"},
    )
    dispatcher = JobFlowDispatcher(None, _Store(), mode="on")
    with pytest.raises(RuntimeError, match="dispatch fenced"):
        dispatcher._dispatch(event)


def test_request_run_refuses_before_resolve_or_jobs_write(monkeypatch):
    import cron.jobs as jobs

    calls = []
    monkeypatch.setattr(
        jobs, "default_control_store", lambda: _FenceStore(calls, refuse=True), raising=False
    )
    monkeypatch.setattr(
        jobs,
        "resolve_job_ref",
        lambda _job_id: pytest.fail("job resolved after fence refusal"),
    )

    with pytest.raises(RuntimeError, match="dispatch fenced"):
        jobs.request_run("job-1", caller="test")


def test_trigger_job_refuses_before_resolve_or_jobs_write(monkeypatch):
    import cron.jobs as jobs

    calls = []
    monkeypatch.setattr(
        jobs, "default_control_store", lambda: _FenceStore(calls, refuse=True), raising=False
    )
    monkeypatch.setattr(
        jobs,
        "resolve_job_ref",
        lambda _job_id: pytest.fail("job resolved after fence refusal"),
    )

    with pytest.raises(RuntimeError, match="dispatch fenced"):
        jobs.trigger_job("job-1", caller="test")


def test_dispatcher_section_spans_claim_through_wake(monkeypatch):
    from events.schema import Event, EventType, Priority
    from events.subscribers.jobflow_dispatcher import JobFlowDispatcher
    import events.subscribers.jobflow_dispatcher as dispatcher_module

    calls = []
    monkeypatch.setattr(
        dispatcher_module, "default_control_store", lambda: _FenceStore(calls), raising=False
    )

    class _Outbox:
        job_id = "job-1"
        caller = "jobflow-dispatcher"
        reason = "mailbox_message"
        activity_id = "jobflow.matcher"

    class _Store:
        lease_seconds = 10

        def claim_for_wake(self, *_a, **_k):
            calls.append("claim")
            return _Outbox()

        def ack_wake_outbox(self, _outbox):
            calls.append("ack")
            return True

    event = Event(
        event_id="event-1",
        event_type=EventType.MAILBOX_MESSAGE,
        timestamp="2026-08-21T00:00:00Z",
        source="test",
        priority=Priority.NORMAL,
        payload={"message_type": "SCORE_REQUEST", "to": "matcher", "file": "matcher/inbox/m1.json"},
    )
    dispatcher = JobFlowDispatcher(
        None,
        _Store(),
        mode="on",
        resolve_job_id=lambda _activity: calls.append("resolve") or "job-1",
        waker=lambda *_a, **_k: calls.append("wake") or True,
    )
    dispatcher._dispatch(event)

    assert calls.index(("enter", "jobflow-dispatcher")) < calls.index("claim")
    assert calls.index("wake") < calls.index(("exit", "jobflow-dispatcher"))


# --- Retained-admission ownership handoff -----------------------------------
#
# ``_execute_job_now`` and ``fire_due`` enter an admission and hand it to
# ``run_one_job``, which releases it at the durable-running handoff. Both used
# to infer that handoff from control flow -- ``handed_off = True`` on the line
# BEFORE the call, with the caller's ``finally`` skipped on it. That is correct
# for every path that reaches ``run_one_job``'s body and wrong for the one that
# does not: an argument-binding ``TypeError`` leaves the callee's ``finally``
# unarmed and the caller's disarmed, so the section is held by nobody. Observed
# for real via test doubles (five ``def _fake(job)`` stubs in
# tests/hermes_cli/test_console_engine.py, d5722113ab); reachable in production
# through signature skew between a partially-deployed scheduler and its callers.


class _RetainingFenceStore(_FenceStore):
    """A ``_FenceStore`` that keeps every section it hands out alive.

    Without this, these tests cannot see the bug they exist for.
    ``@contextmanager`` sections are cleaned up by CPython refcounting: drop the
    last reference to an un-exited one and the generator is finalized,
    ``GeneratorExit`` runs its ``finally``, and the section releases anyway.
    That safety net is real but it is not the contract -- it fires at an
    arbitrary later moment, and it does not fire at all while something still
    holds the frame, which is precisely the leaking path (``fire_due`` lets the
    TypeError propagate and its traceback pins the frame that owns the admission
    for as long as the exception lives).

    Retaining the sections removes the net so the ownership handoff itself is
    what the assertions measure. Verified against the pre-fix code: with the net
    left in place, these tests passed on the buggy version too.
    """

    def __init__(self, calls, *, refuse=False):
        super().__init__(calls, refuse=refuse)
        self.sections = []

    def dispatch_section(self, *, boundary):
        section = super().dispatch_section(boundary=boundary)
        self.sections.append(section)
        return section


def _binding_mismatch_run_one_job(job, *, adapters=None, loop=None, verbose=False):
    """``run_one_job``'s signature as it stood before 410c57ddc9 added the kwarg.

    Passing ``_dispatch_admission=`` to it raises TypeError at BINDING -- no
    frame of this function ever executes -- which is exactly the path that used
    to leak the admission.
    """
    raise AssertionError("body must not run; the call cannot even bind")


def test_manual_fire_releases_admission_when_run_one_job_cannot_bind(monkeypatch):
    import cron.scheduler as scheduler
    import jobflow_dispatch.quarantine_control as quarantine_control
    import tools.cronjob_tools as cronjob_tools

    calls = []
    monkeypatch.setattr(
        quarantine_control, "default_control_store", lambda: _RetainingFenceStore(calls)
    )
    monkeypatch.setattr(cronjob_tools, "claim_job_for_fire", lambda _jid: True)
    monkeypatch.setattr(cronjob_tools, "get_job", lambda jid: {"id": jid, "name": "one"})
    monkeypatch.setattr(cronjob_tools, "emit_cron_triggered_safe", lambda **_k: None)
    monkeypatch.setattr(cronjob_tools, "mark_job_run", lambda *_a, **_k: None)
    monkeypatch.setattr(scheduler, "run_one_job", _binding_mismatch_run_one_job)

    result = cronjob_tools._execute_job_now({"id": "job-1", "name": "one"})

    assert result["success"] is False
    assert "_dispatch_admission" in (result["error"] or "")
    # The point of the test: the caller's finally still ran the release.
    assert calls.count(("exit", "manual-immediate-fire")) == 1


def test_provider_fire_releases_admission_when_run_one_job_cannot_bind(monkeypatch):
    import cron.executions as executions
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    import cron.scheduler_provider as provider

    calls = []
    monkeypatch.setattr(
        provider, "default_control_store", lambda: _RetainingFenceStore(calls)
    )
    monkeypatch.setattr(jobs, "claim_job_for_fire", lambda _jid: True)
    monkeypatch.setattr(jobs, "get_job", lambda jid: {"id": jid, "name": "one"})
    monkeypatch.setattr(executions, "create_execution", lambda *_a, **_k: {"id": "exec-1"})
    monkeypatch.setattr(scheduler, "run_one_job", _binding_mismatch_run_one_job)

    # fire_due has no except-handler, so the binding failure propagates -- the
    # release must happen on the way out rather than riding on the dead frame.
    with pytest.raises(TypeError, match="_dispatch_admission"):
        provider.InProcessCronScheduler().fire_due("job-1")

    assert calls.count(("exit", "external-provider-fire")) == 1


def test_manual_fire_does_not_double_release_the_handed_off_admission(monkeypatch):
    """The normal path: the callee releases, the caller's finally is a no-op.

    ``run_one_job`` releases on BOTH its success and BaseException paths, so a
    caller that released again would exit the section twice -- and a second exit
    must never land on whatever section a later frame had since entered.
    """
    import cron.scheduler as scheduler
    import jobflow_dispatch.quarantine_control as quarantine_control
    import tools.cronjob_tools as cronjob_tools

    calls = []
    monkeypatch.setattr(
        quarantine_control, "default_control_store", lambda: _RetainingFenceStore(calls)
    )
    monkeypatch.setattr(cronjob_tools, "claim_job_for_fire", lambda _jid: True)
    monkeypatch.setattr(
        cronjob_tools,
        "get_job",
        lambda jid: {"id": jid, "name": "one", "last_status": "ok", "last_error": None},
    )
    monkeypatch.setattr(cronjob_tools, "emit_cron_triggered_safe", lambda **_k: None)
    monkeypatch.setattr(cronjob_tools, "mark_job_run", lambda *_a, **_k: None)

    def handoff(_job, *, _dispatch_admission=None, **_kwargs):
        calls.append("handoff")
        _dispatch_admission.__exit__(None, None, None)
        return True

    monkeypatch.setattr(scheduler, "run_one_job", handoff)

    result = cronjob_tools._execute_job_now({"id": "job-1", "name": "one"})

    assert result["success"] is True
    assert calls.count(("exit", "manual-immediate-fire")) == 1
    assert calls.index("handoff") < calls.index(("exit", "manual-immediate-fire"))


def test_retained_admission_release_is_idempotent_across_frames():
    from jobflow_dispatch.quarantine_control import retain_dispatch_admission

    calls = []
    admission = retain_dispatch_admission(_RetainingFenceStore(calls), boundary="unit")
    assert admission.released is False

    admission.__exit__(None, None, None)  # the callee's handoff release
    assert admission.released is True
    admission.release()  # the caller's finally
    admission.release()  # and any number of frames after it

    assert calls == [("enter", "unit"), ("exit", "unit")]


def test_retained_admission_stays_released_when_teardown_raises():
    """A section whose teardown raises is still torn down -- never re-entered."""

    class _ExplodingSection:
        def __init__(self):
            self.exits = 0

        def __enter__(self):
            return None

        def __exit__(self, *_exc):
            self.exits += 1
            raise RuntimeError("teardown failed")

    from jobflow_dispatch.quarantine_control import RetainedDispatchAdmission

    section = _ExplodingSection()
    admission = RetainedDispatchAdmission(section).__enter__()

    with pytest.raises(RuntimeError, match="teardown failed"):
        admission.__exit__(None, None, None)

    admission.release()  # the caller's finally must not retry it
    assert section.exits == 1
