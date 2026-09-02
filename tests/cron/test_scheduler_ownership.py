"""Behavioral coverage for Desktop versus gateway cron ownership."""

from pathlib import Path


def test_service_owned_profile_disables_desktop_even_if_lock_probe_breaks(monkeypatch):
    import cron.scheduler as scheduler
    import gateway.status as gateway_status

    monkeypatch.setattr(
        scheduler,
        "load_config",
        lambda: {"cron": {"desktop_fallback": False}},
    )

    def _unexpected_probe(_path):
        raise AssertionError("explicit fail-closed policy must short-circuit the probe")

    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", _unexpected_probe)

    assert scheduler.desktop_scheduler_may_dispatch() is False


def test_desktop_defers_to_same_profile_gateway_lock(monkeypatch, tmp_path):
    import cron.scheduler as scheduler
    import gateway.status as gateway_status

    seen = []
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        gateway_status,
        "is_gateway_runtime_lock_active",
        lambda path: seen.append(path) or True,
    )

    assert scheduler.desktop_scheduler_may_dispatch() is False
    assert seen == [tmp_path / "gateway.lock"]


def test_desktop_fallback_runs_without_gateway(monkeypatch, tmp_path):
    import cron.scheduler as scheduler
    import gateway.status as gateway_status

    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        gateway_status,
        "is_gateway_runtime_lock_active",
        lambda _path: False,
    )

    assert scheduler.desktop_scheduler_may_dispatch() is True


def test_desktop_probe_failure_preserves_default_fallback(monkeypatch, tmp_path):
    import cron.scheduler as scheduler
    import gateway.status as gateway_status

    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    monkeypatch.setattr(scheduler, "get_hermes_home", lambda: tmp_path)

    def _broken_probe(_path):
        raise PermissionError("cannot inspect lock")

    monkeypatch.setattr(gateway_status, "is_gateway_runtime_lock_active", _broken_probe)

    assert scheduler.desktop_scheduler_may_dispatch() is True


def test_tick_guard_leaves_due_jobs_untouched(monkeypatch, tmp_path):
    import cron.scheduler as scheduler

    monkeypatch.setattr(
        scheduler,
        "_get_lock_paths",
        lambda: (tmp_path / "cron", tmp_path / "cron" / ".tick.lock"),
    )
    monkeypatch.setattr(scheduler, "desktop_scheduler_may_dispatch", lambda: False)

    def _unexpected_due_read():
        raise AssertionError("deferred Desktop tick must not inspect due jobs")

    monkeypatch.setattr(scheduler, "get_due_jobs", _unexpected_due_read)

    assert scheduler.tick(verbose=False, desktop_owner_guard=True) == 0


class _OneWaitStop:
    def __init__(self):
        self.waits = 0

    def is_set(self):
        return self.waits > 0

    def wait(self, _interval):
        self.waits += 1
        return True


def test_guarded_provider_does_not_recover_heartbeat_or_tick(monkeypatch):
    import cron.jobs as jobs
    import cron.scheduler as scheduler
    from cron.scheduler_provider import InProcessCronScheduler

    provider = InProcessCronScheduler()
    stop = _OneWaitStop()
    monkeypatch.setattr(scheduler, "desktop_scheduler_may_dispatch", lambda: False)

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("deferred Desktop provider touched scheduler state")

    monkeypatch.setattr(provider, "recover_interrupted", _unexpected)
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", _unexpected)
    monkeypatch.setattr(scheduler, "tick", _unexpected)

    provider.start(stop, interval=1, desktop_owner_guard=True)

    assert stop.waits == 1
