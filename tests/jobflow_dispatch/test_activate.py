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
