"""Multiplex Gateway cron ownership is resolved independently per profile."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from cron.scheduler_runtime import SchedulerOwnershipPolicy


def test_mixed_multiplex_profiles_start_supervisors_for_every_exact_home(
    tmp_path, monkeypatch
):
    import gateway.run as gateway_run

    homes = [
        ("default", tmp_path / "default"),
        ("desktop", tmp_path / "desktop"),
        ("chronos", tmp_path / "chronos"),
        ("malformed", tmp_path / "malformed"),
    ]
    policies = {
        homes[0][1].resolve(): SchedulerOwnershipPolicy("auto", "builtin"),
        homes[1][1].resolve(): SchedulerOwnershipPolicy("desktop", "builtin"),
        homes[2][1].resolve(): SchedulerOwnershipPolicy("gateway", "chronos"),
        homes[3][1].resolve(): None,
    }
    reads = []
    constructed = []

    def read_policy(*, hermes_home, **_kwargs):
        home = Path(hermes_home).resolve()
        reads.append(home)
        return policies[home]

    class FakeRuntime:
        def __init__(self, owner, **kwargs):
            constructed.append((owner, kwargs["hermes_home"]))

        def run(self, _stop):
            return None

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve", lambda multiplex: homes
    )
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        read_policy,
    )
    monkeypatch.setattr("cron.scheduler_runtime.OwnedSchedulerRuntime", FakeRuntime)

    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True),
        adapters={},
        _draining=False,
        _external_drain_active=False,
    )
    supervisors = gateway_run._start_gateway_cron_schedulers(
        runner, threading.Event(), None
    )
    for _runtime, thread in supervisors:
        thread.join(1)

    assert reads == []
    assert constructed == [("gateway", home.resolve()) for _, home in homes]
    assert len(supervisors) == 4
