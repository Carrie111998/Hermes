"""Tests for the ``cron_job_failed`` lifecycle hook.

The hook fires from ``cron/scheduler.py::run_one_job`` when a job fails
(error surfaced, success=False), so reactive consumers (shell hooks,
outbound webhooks, plugins) get a programmatic failure signal without
polling ``jobs.json``.  It must:

* fire exactly once per failed run, with the full job payload
* NOT fire on success
* never raise into the scheduler (a broken hook cannot crash the job loop)
"""

import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, error=None):
    """Patch the job pipeline primitives (same shape as test_run_one_job)."""

    def fake_run_job(job):
        return (success, "out", "final response", error)

    def fake_save(jid, out):
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None):
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)


def test_failed_job_fires_cron_job_failed_hook(monkeypatch):
    """A failing job fires the hook with the job spec and error text."""
    _patch_pipeline(monkeypatch, success=False, error="boom")
    captured = {}

    def fake_hook(job, err):
        captured["job"] = job
        captured["err"] = err

    monkeypatch.setattr(s, "_fire_cron_job_failed_hook", fake_hook)

    job = {"id": "jfail", "name": "nightly", "profile": "work"}
    s.run_one_job(job)

    assert captured["job"] is job
    assert captured["err"] == "boom"


def test_successful_job_does_not_fire_hook(monkeypatch):
    """A successful job must NOT fire the failure hook."""
    _patch_pipeline(monkeypatch, success=True)
    fired = []

    def fake_hook(job, err):
        fired.append((job, err))

    monkeypatch.setattr(s, "_fire_cron_job_failed_hook", fake_hook)

    ok = s.run_one_job({"id": "jok", "name": "ok-job"})

    assert ok is True
    assert fired == []


def test_hook_exception_does_not_crash_scheduler(monkeypatch):
    """A raising hook is swallowed: the job still completes without raising."""
    _patch_pipeline(monkeypatch, success=False, error="boom")

    def exploding_hook(job, err):
        raise RuntimeError("hook script exploded")

    monkeypatch.setattr(s, "_fire_cron_job_failed_hook", exploding_hook)

    # run_one_job must not raise even though the hook raises.
    s.run_one_job({"id": "jboom", "name": "boom-job"})


def test_real_hook_function_uses_plugins_invoke_hook(monkeypatch):
    """The real _fire_cron_job_failed_hook dispatches through
    hermes_cli.plugins.invoke_hook and swallows its own failures."""
    import hermes_cli.plugins as plugins

    calls = []

    def fake_plugins_invoke_hook(name, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(plugins, "invoke_hook", fake_plugins_invoke_hook)

    job = {"id": "jreal", "name": "real-job", "profile": "ops", "last_run_at": "2026-08-10T00:00:00Z"}
    s._fire_cron_job_failed_hook(job, "kaput")

    assert len(calls) == 1
    name, kwargs = calls[0]
    assert name == "cron_job_failed"
    assert kwargs["job_id"] == "jreal"
    assert kwargs["error"] == "kaput"
    assert kwargs["job"] is job


def test_real_hook_function_swallows_exception(monkeypatch):
    """A failure inside plugins.invoke_hook must be logged, not raised."""
    import hermes_cli.plugins as plugins

    def raising_invoke_hook(name, **kwargs):
        raise RuntimeError("plugin dispatch failed")

    monkeypatch.setattr(plugins, "invoke_hook", raising_invoke_hook)

    # Must not raise.
    s._fire_cron_job_failed_hook({"id": "jx", "name": "x"}, "err")
