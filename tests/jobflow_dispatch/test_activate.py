"""Resolving an activity to the one enabled cron job that serves it.

Fail-closed on zero OR multiple is the whole point: activating the wrong
worker is worse than not activating one, because the next reconcile catches
the miss. Tests use REAL activity IDs from activity_policy/policies.yaml so a
rename of an alias breaks here rather than in production.
"""

from __future__ import annotations

import pytest

from jobflow_dispatch.activate import resolve_job_id_for_activity


def _job(name, job_id, enabled=True):
    return {"id": job_id, "name": name, "enabled": enabled}


class TestResolveJobIdForActivity:
    def test_resolves_a_single_enabled_job(self, monkeypatch):
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [_job("jobflow-tailor", "b95c7eba034a")],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") == "b95c7eba034a"

    def test_refuses_when_the_only_match_is_disabled(self, monkeypatch):
        """The hazard this whole change exists to close."""
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [_job("jobflow-tailor", "b95c7eba034a", enabled=False)],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_refuses_when_no_job_matches(self, monkeypatch):
        monkeypatch.setattr("cron.jobs.load_jobs", lambda: [])
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_refuses_when_two_enabled_jobs_match(self, monkeypatch):
        """Refuse to guess rather than pick the first."""
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [
                _job("jobflow-tailor", "aaaaaaaaaaaa"),
                _job("jobflow-tailor", "bbbbbbbbbbbb"),
            ],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_unknown_activity_returns_none(self, monkeypatch):
        monkeypatch.setattr("cron.jobs.load_jobs", lambda: [])
        assert resolve_job_id_for_activity("no.such.activity") is None


def test_dispatcher_still_exposes_the_resolver():
    """The subscriber's import must survive the move — it is its default arg."""
    from events.subscribers import jobflow_dispatcher

    assert (
        jobflow_dispatcher.resolve_job_id_for_activity is resolve_job_id_for_activity
    )


from jobflow_dispatch.activate import ActivationReport, activate_pending
from jobflow_dispatch.contracts import Activation


def _act(activity_id, key="tailor/inbox/m1.json"):
    return Activation(
        activity_id=activity_id,
        profile="main",
        message_key=key,
        correlation_id=None,
        reason="reconcile",
    )


class _Recorder:
    """Stand-in for cron.jobs.request_run that records how it was called."""

    def __init__(self, refuse=(), raise_for=()):
        self.calls = []
        self._refuse = set(refuse)
        self._raise_for = set(raise_for)

    def __call__(self, job_id, *, caller, reason=None):
        self.calls.append((job_id, caller, reason))
        if job_id in self._raise_for:
            raise RuntimeError("boom")
        if job_id in self._refuse:
            return None
        return {"id": job_id, "next_run_at": "2026-08-11T00:30:00-04:00"}


class TestActivatePending:
    def test_activates_each_resolved_job_once(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: {"a.one": "job-1", "a.two": "job-2"}[a],
            request_run=runner,
        )

        assert [c[0] for c in runner.calls] == ["job-1", "job-2"]
        assert report.activated == ("job-1", "job-2")
        assert report.activations == 2
        assert report.activities == 2
        assert report.needs_agent is False

    def test_many_activations_for_one_job_wake_it_once(self):
        """Trigger each distinct job at most once per run, however many
        activations map to it."""
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", f"k{i}") for i in range(5)],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert len(runner.calls) == 1
        assert report.activations == 5
        assert report.activities == 1
        assert report.activated == ("job-1",)

    def test_two_activities_sharing_one_job_wake_it_once(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert len(runner.calls) == 1
        assert report.activated == ("job-1",)
        assert report.needs_agent is False

    def test_unresolved_activity_is_counted_and_never_activated(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one")],
            resolve=lambda a: None,
            request_run=runner,
        )

        assert runner.calls == []
        assert report.unresolved == ("a.one",)
        assert report.activated == ()
        assert report.needs_agent is True

    def test_job_disabled_between_scan_and_activation_is_refused(self):
        """The TOCTOU case: resolution succeeded, request_run said no."""
        runner = _Recorder(refuse={"job-1"})
        report = activate_pending(
            [_act("a.one")],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert report.refused == ("a.one",)
        assert report.activated == ()
        assert report.needs_agent is True

    def test_one_failure_does_not_prevent_the_others(self):
        runner = _Recorder(raise_for={"job-1"})
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: {"a.one": "job-1", "a.two": "job-2"}[a],
            request_run=runner,
        )

        assert report.errors == ("a.one",)
        assert report.activated == ("job-2",)
        assert report.needs_agent is True

    def test_a_raising_resolver_is_isolated_too(self):
        runner = _Recorder()

        def resolve(activity_id):
            if activity_id == "a.one":
                raise RuntimeError("registry broken")
            return "job-2"

        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=resolve,
            request_run=runner,
        )

        assert report.errors == ("a.one",)
        assert report.activated == ("job-2",)

    def test_attribution_is_stable(self):
        """Activations must be reconstructable from the audit log."""
        runner = _Recorder()
        activate_pending(
            [_act("a.one")], resolve=lambda a: "job-1", request_run=runner
        )

        assert runner.calls == [("job-1", "cron:jobflow-reconcile", "reconcile")]

    def test_empty_input_is_a_clean_silent_report(self):
        report = activate_pending([], resolve=lambda a: "job-1", request_run=_Recorder())
        assert report == ActivationReport(0, 0, (), (), (), ())
        assert report.needs_agent is False
