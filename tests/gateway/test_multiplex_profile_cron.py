"""Regression tests for multiplex gateway profile-local cron scheduling.

A default-profile multiplex gateway owns the live adapters for secondary
profiles.  It must tick each secondary profile's isolated cron store under that
profile's runtime scope; starting only the default ticker leaves those jobs
permanently scheduled but never executed.
"""

from pathlib import Path
import threading


def test_profile_cron_provider_runs_in_isolated_profile_scope(tmp_path):
    from gateway.run import _run_profile_cron_provider
    from hermes_constants import (
        get_hermes_home,
        reset_hermes_home_override,
        set_hermes_home_override,
    )
    from cron.jobs import get_cron_output_dir
    from agent.secret_scope import get_secret

    root = tmp_path / "root"
    profile = root / "profiles" / "polymarket"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("PROFILE_CRON_SENTINEL=polymarket-only\n")
    adapters = {"telegram": object()}
    seen = {}

    class FakeProvider:
        def start(self, stop_event, **kwargs):
            seen["home"] = get_hermes_home().resolve()
            seen["output"] = get_cron_output_dir().resolve()
            seen["secret"] = get_secret("PROFILE_CRON_SENTINEL")
            seen["adapters"] = kwargs["adapters"]
            seen["can_dispatch"] = kwargs["can_dispatch"]()

    root_token = set_hermes_home_override(str(root))
    try:
        _run_profile_cron_provider(
            "polymarket",
            profile,
            FakeProvider(),
            threading.Event(),
            adapters=adapters,
            loop=object(),
            can_dispatch=lambda: True,
        )
        assert seen == {
            "home": profile.resolve(),
            "output": (profile / "cron" / "output").resolve(),
            "secret": "polymarket-only",
            "adapters": adapters,
            "can_dispatch": True,
        }
        assert get_hermes_home().resolve() == root.resolve()
        assert get_secret("PROFILE_CRON_SENTINEL") is None
    finally:
        reset_hermes_home_override(root_token)


def test_secondary_profile_cron_threads_use_profile_adapters(tmp_path, monkeypatch):
    from gateway import run

    profile = tmp_path / "profiles" / "polymarket"
    profile.mkdir(parents=True)
    profile_adapters = {"telegram": object()}
    default_adapters = {"telegram": object()}
    starts = []

    class FakeProvider:
        def start(self, *args, **kwargs):
            raise AssertionError("thread target must not execute in construction test")

    class FakeThread:
        def __init__(self, *, target, args, kwargs, daemon, name):
            starts.append({
                "target": target,
                "args": args,
                "kwargs": kwargs,
                "daemon": daemon,
                "name": name,
            })
        def start(self):
            starts[-1]["started"] = True

    class Runner:
        adapters = default_adapters
        _profile_adapters = {"polymarket": profile_adapters}
        _draining = False
        _external_drain_active = False

    monkeypatch.setattr(run.threading, "Thread", FakeThread)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile)
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: FakeProvider())

    handles = run._start_secondary_profile_cron_schedulers(
        Runner(), threading.Event(), loop=object()
    )

    assert len(handles) == 1
    assert starts[0]["started"] is True
    assert starts[0]["name"] == "cron-scheduler-polymarket"
    assert starts[0]["args"][0] == "polymarket"
    assert starts[0]["args"][1] == profile
    assert starts[0]["kwargs"]["adapters"] is profile_adapters
    assert starts[0]["kwargs"]["adapters"] is not default_adapters
