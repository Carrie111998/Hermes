"""Profile-scoped cron scheduler ownership lease regressions.

The scheduler owner is distinct from a job fire claim. It decides which runtime
may inspect and dispatch one profile's cron store. Job claims still provide the
per-fire at-most-once backstop.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest


def test_two_gateway_schedulers_racing_for_one_profile_have_one_owner(tmp_path):
    from cron.scheduler_ownership import SchedulerOwnershipLease

    profile_home = tmp_path / "profiles" / "brand"
    barrier = threading.Barrier(2)

    def claim(runtime_id: str):
        lease = SchedulerOwnershipLease(
            profile_home=profile_home,
            profile="brand",
            runtime_id=runtime_id,
            owner_kind="gateway-dedicated",
            lease_seconds=30,
        )
        barrier.wait(timeout=5)
        return lease, lease.claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("brand-gateway-a", "brand-gateway-b")))

    assert sum(int(claimed) for _lease, claimed in results) == 1
    winner = next(lease for lease, claimed in results if claimed)
    loser = next(lease for lease, claimed in results if not claimed)
    assert winner.is_owner() is True
    assert loser.is_owner() is False


def test_dedicated_gateway_atomically_preempts_desktop_fallback(tmp_path):
    from cron.scheduler_ownership import SchedulerOwnershipLease

    profile_home = tmp_path / "profiles" / "brand"
    desktop = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="aya-desktop",
        owner_kind="desktop-fallback",
        lease_seconds=30,
    )
    dedicated = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="brand-gateway",
        owner_kind="gateway-dedicated",
        lease_seconds=30,
    )

    assert desktop.claim() is True
    assert dedicated.claim() is True
    assert dedicated.is_owner() is True
    assert desktop.is_owner() is False

    with desktop.dispatch_guard() as allowed:
        assert allowed is False
    with dedicated.dispatch_guard() as allowed:
        assert allowed is True


def test_equal_priority_takeover_waits_for_expiry(tmp_path):
    from cron.scheduler_ownership import SchedulerOwnershipLease

    profile_home = tmp_path / "profiles" / "brand"
    now = [1000.0]
    first = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="brand-gateway-old",
        owner_kind="gateway-dedicated",
        lease_seconds=10,
        clock=lambda: now[0],
    )
    successor = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="brand-gateway-new",
        owner_kind="gateway-dedicated",
        lease_seconds=10,
        clock=lambda: now[0],
    )

    assert first.claim() is True
    now[0] = 1009.9
    assert successor.claim() is False
    now[0] = 1010.1
    assert successor.claim() is True
    assert successor.is_owner() is True
    assert first.renew() is False


def test_release_is_token_guarded_and_allows_controlled_takeover(tmp_path):
    from cron.scheduler_ownership import SchedulerOwnershipLease

    profile_home = tmp_path / "profiles" / "brand"
    owner = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="brand-gateway-old",
        owner_kind="gateway-dedicated",
    )
    contender = SchedulerOwnershipLease(
        profile_home=profile_home,
        profile="brand",
        runtime_id="brand-gateway-new",
        owner_kind="gateway-dedicated",
    )

    assert owner.claim() is True
    assert contender.release() is False
    assert contender.claim() is False
    assert owner.release() is True
    assert contender.claim() is True


def test_dispatch_guard_preserves_exception_from_guarded_body(tmp_path):
    from cron.scheduler_ownership import SchedulerOwnershipLease

    lease = SchedulerOwnershipLease(
        profile_home=tmp_path,
        profile="brand",
        runtime_id="brand-gateway",
        owner_kind="gateway-dedicated",
    )
    assert lease.claim() is True

    with pytest.raises(ValueError, match="body failed"):
        with lease.dispatch_guard() as allowed:
            assert allowed is True
            raise ValueError("body failed")


def test_dedicated_preemption_retries_promptly_after_busy_guard(tmp_path, monkeypatch):
    from cron.scheduler_ownership import SchedulerOwnershipLease
    from cron.scheduler_provider import InProcessCronScheduler

    home = tmp_path / ".hermes" / "profiles" / "brand"
    (home / "cron").mkdir(parents=True)
    desktop = SchedulerOwnershipLease(
        profile_home=home,
        profile="brand",
        runtime_id="desktop",
        owner_kind="desktop-fallback",
    )
    assert desktop.claim() is True

    guard_entered = threading.Event()
    release_guard = threading.Event()

    def hold_desktop_guard():
        with desktop.dispatch_guard() as allowed:
            assert allowed is True
            guard_entered.set()
            release_guard.wait(timeout=10)

    holder = threading.Thread(target=hold_desktop_guard)
    holder.start()
    assert guard_entered.wait(timeout=5)

    dispatched = threading.Event()
    stop = threading.Event()

    def tracking_tick(*_args, **_kwargs):
        dispatched.set()
        stop.set()
        return 0

    monkeypatch.setattr("cron.scheduler.tick", tracking_tick)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: None)
    scheduler_thread = threading.Thread(
        target=InProcessCronScheduler().start,
        args=(stop,),
        kwargs={
            "interval": 60,
            "profile_homes": [("brand", home)],
            "owner_kind": "gateway-dedicated",
            "runtime_id": "brand-gateway",
        },
    )
    scheduler_thread.start()
    # Force both the initial claim and first loop claim past the one-second
    # lock timeout, then make ownership available.
    time.sleep(2.3)
    release_guard.set()

    try:
        assert dispatched.wait(timeout=3), (
            "dedicated gateway did not retry ownership promptly after Desktop released"
        )
    finally:
        stop.set()
        release_guard.set()
        holder.join(timeout=5)
        scheduler_thread.join(timeout=5)
    assert not holder.is_alive()
    assert not scheduler_thread.is_alive()


def test_identical_job_ids_in_two_profiles_track_independently(tmp_path):
    from cron.scheduler import (
        get_running_cron_runs,
        is_cron_job_running,
        release_running_job,
        try_register_running_job,
    )

    brand = tmp_path / "profiles" / "brand"
    scout = tmp_path / "profiles" / "scout"
    assert try_register_running_job("same-id", profile_home=brand) is True
    assert try_register_running_job("same-id", profile_home=scout) is True
    try:
        assert is_cron_job_running("same-id", profile_home=brand) is True
        assert is_cron_job_running("same-id", profile_home=scout) is True
        assert len(get_running_cron_runs()) == 2
        release_running_job("same-id", profile_home=brand)
        assert is_cron_job_running("same-id", profile_home=brand) is False
        assert is_cron_job_running("same-id", profile_home=scout) is True
    finally:
        release_running_job("same-id", profile_home=brand)
        release_running_job("same-id", profile_home=scout)


def test_two_scheduler_instances_racing_dispatch_once_then_transfer(
    tmp_path,
    monkeypatch,
):
    from cron.scheduler_provider import InProcessCronScheduler

    profile_home = tmp_path / ".hermes" / "profiles" / "brand"
    (profile_home / "cron").mkdir(parents=True)
    starts = threading.Barrier(2)
    stops = [threading.Event(), threading.Event()]
    dispatched = []

    def tracking_tick(*_args, **_kwargs):
        dispatched.append(threading.current_thread().name)
        for event in stops:
            event.set()
        return 0

    def run(index: int):
        starts.wait(timeout=5)
        InProcessCronScheduler().start(
            stops[index],
            interval=0,
            profile_homes=[("brand", profile_home)],
            owner_kind="gateway-dedicated",
            runtime_id=f"brand-gateway-{index}",
        )

    monkeypatch.setattr("cron.scheduler.tick", tracking_tick)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: None)

    threads = [
        threading.Thread(target=run, args=(index,), name=f"scheduler-{index}")
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert len(dispatched) == 1

    successor_stop = threading.Event()

    def successor_tick(*_args, **_kwargs):
        dispatched.append("successor")
        successor_stop.set()
        return 0

    monkeypatch.setattr("cron.scheduler.tick", successor_tick)
    InProcessCronScheduler().start(
        successor_stop,
        interval=0,
        profile_homes=[("brand", profile_home)],
        owner_kind="gateway-dedicated",
        runtime_id="brand-gateway-successor",
    )

    assert dispatched == [dispatched[0], "successor"]


def test_external_provider_equal_priority_race_starts_one_owner(tmp_path):
    from cron.scheduler_provider import CronScheduler, bind_external_scheduler_ownership

    starts = []

    class External(CronScheduler):
        def __init__(self, label):
            self.label = label

        @property
        def name(self):
            return "external-test"

        def start(self, stop_event, **_kwargs):
            starts.append(self.label)

    stop_a = threading.Event()
    stop_b = threading.Event()
    owner_a = bind_external_scheduler_ownership(
        External("a"),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="gateway-a",
        owner_kind="gateway-dedicated",
        poll_interval=0.02,
    )
    owner_b = bind_external_scheduler_ownership(
        External("b"),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="gateway-b",
        owner_kind="gateway-dedicated",
        poll_interval=0.02,
    )
    threads = [
        threading.Thread(target=owner_a.start, args=(stop_a,)),
        threading.Thread(target=owner_b.start, args=(stop_b,)),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while not starts and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)

    assert len(starts) == 1

    stop_a.set()
    stop_b.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_external_dedicated_gateway_preempts_desktop_and_guards_hooks(tmp_path):
    from cron.scheduler_provider import CronScheduler, bind_external_scheduler_ownership

    events = []

    class External(CronScheduler):
        def __init__(self, label):
            self.label = label

        @property
        def name(self):
            return "external-test"

        def start(self, stop_event, **_kwargs):
            events.append((self.label, "start"))

        def on_jobs_changed(self):
            events.append((self.label, "changed"))

    desktop_stop = threading.Event()
    gateway_stop = threading.Event()
    desktop = bind_external_scheduler_ownership(
        External("desktop"),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="desktop",
        owner_kind="desktop-fallback",
        poll_interval=0.02,
    )
    gateway = bind_external_scheduler_ownership(
        External("gateway"),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="gateway",
        owner_kind="gateway-dedicated",
        poll_interval=0.02,
    )
    desktop_thread = threading.Thread(target=desktop.start, args=(desktop_stop,))
    gateway_thread = threading.Thread(target=gateway.start, args=(gateway_stop,))
    desktop_thread.start()
    deadline = time.monotonic() + 2
    while ("desktop", "start") not in events and time.monotonic() < deadline:
        time.sleep(0.01)
    gateway_thread.start()
    deadline = time.monotonic() + 2
    while ("gateway", "start") not in events and time.monotonic() < deadline:
        time.sleep(0.01)

    desktop.on_jobs_changed()
    gateway.on_jobs_changed()

    assert ("gateway", "start") in events
    assert ("desktop", "changed") not in events
    assert ("gateway", "changed") in events

    desktop_stop.set()
    gateway_stop.set()
    desktop_thread.join(timeout=5)
    gateway_thread.join(timeout=5)
    assert not desktop_thread.is_alive()
    assert not gateway_thread.is_alive()


def test_external_wrapper_preserves_legacy_single_phase_fire(tmp_path):
    from cron.scheduler_provider import (
        CronScheduler,
        bind_external_scheduler_ownership,
        provider_supports_split_fire,
    )

    calls = []

    class LegacyExternal(CronScheduler):
        @property
        def name(self):
            return "legacy"

        def start(self, stop_event, **_kwargs):
            return None

        def fire_due(self, job_id, *, adapters=None, loop=None):
            calls.append(job_id)
            return True

    raw = LegacyExternal()
    wrapped = bind_external_scheduler_ownership(
        raw,
        profile_home=tmp_path,
        profile="brand",
        runtime_id="gateway",
        owner_kind="gateway-dedicated",
        poll_interval=0.02,
    )
    assert provider_supports_split_fire(raw) is False
    assert provider_supports_split_fire(wrapped) is False

    stop = threading.Event()
    thread = threading.Thread(target=wrapped.start, args=(stop,))
    thread.start()
    deadline = time.monotonic() + 2
    while not wrapped._lease.is_owner() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert wrapped.fire_due("job-1") is True
        assert calls == ["job-1"]
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_blocking_external_start_does_not_block_dedicated_preemption(tmp_path):
    from cron.scheduler_provider import CronScheduler, bind_external_scheduler_ownership

    desktop_started = threading.Event()
    gateway_started = threading.Event()

    class BlockingExternal(CronScheduler):
        def __init__(self, started):
            self.started = started

        @property
        def name(self):
            return "blocking"

        def start(self, stop_event, **_kwargs):
            self.started.set()
            stop_event.wait(timeout=10)

    desktop = bind_external_scheduler_ownership(
        BlockingExternal(desktop_started),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="desktop",
        owner_kind="desktop-fallback",
        poll_interval=0.02,
    )
    gateway = bind_external_scheduler_ownership(
        BlockingExternal(gateway_started),
        profile_home=tmp_path,
        profile="brand",
        runtime_id="gateway",
        owner_kind="gateway-dedicated",
        poll_interval=0.02,
    )
    desktop_stop = threading.Event()
    gateway_stop = threading.Event()
    desktop_thread = threading.Thread(target=desktop.start, args=(desktop_stop,))
    gateway_thread = threading.Thread(target=gateway.start, args=(gateway_stop,))
    desktop_thread.start()
    assert desktop_started.wait(timeout=2)
    gateway_thread.start()
    try:
        assert gateway_started.wait(timeout=3), (
            "blocking Desktop provider prevented dedicated gateway preemption"
        )
    finally:
        desktop_stop.set()
        gateway_stop.set()
        desktop.stop()
        gateway.stop()
        desktop_thread.join(timeout=5)
        gateway_thread.join(timeout=5)
    assert not desktop_thread.is_alive()
    assert not gateway_thread.is_alive()


def test_desktop_fallback_skips_profile_owned_by_live_dedicated_gateway(
    tmp_path,
    monkeypatch,
):
    from cron.scheduler_ownership import SchedulerOwnershipLease
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    default_home = tmp_path / ".hermes"
    brand_home = default_home / "profiles" / "brand"
    monkeypatch.setenv("AYA_ONLY_SECRET", "must-not-reach-brand")
    for home, marker in ((default_home, "aya"), (brand_home, "brand")):
        (home / "cron").mkdir(parents=True)
        (home / ".env").write_text(f"PROFILE_MARKER={marker}\n", encoding="utf-8")

    dedicated = SchedulerOwnershipLease(
        profile_home=brand_home,
        profile="brand",
        runtime_id="brand-gateway",
        owner_kind="gateway-dedicated",
    )
    assert dedicated.claim() is True

    seen = []
    stop = threading.Event()

    def tracking_tick(*_args, **kwargs):
        from agent.secret_scope import current_secret_scope, get_secret
        from cron.jobs import _current_cron_store

        scope = current_secret_scope() or {}
        seen.append(
            {
                "home": get_hermes_home(),
                "store": _current_cron_store().jobs_file,
                "secret": scope.get("PROFILE_MARKER"),
                "foreign_secret": get_secret("AYA_ONLY_SECRET"),
                "adapters": kwargs.get("adapters"),
            }
        )
        stop.set()
        return 0

    monkeypatch.setattr("cron.scheduler.tick", tracking_tick)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: None)

    provider = InProcessCronScheduler()
    provider.start(
        stop,
        interval=0,
        profile_homes=[("default", default_home), ("brand", brand_home)],
        profile_adapters={
            "default": {"identity": "aya"},
            "brand": {"identity": "brand"},
        },
        owner_kind="desktop-fallback",
        runtime_id="aya-desktop",
    )

    assert seen == [
        {
            "home": default_home,
            "store": default_home / "cron" / "jobs.json",
            "secret": "aya",
            "foreign_secret": None,
            "adapters": {"identity": "aya"},
        }
    ]


def test_dedicated_profile_dispatch_uses_its_complete_runtime_scope(tmp_path, monkeypatch):
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    brand_home = tmp_path / ".hermes" / "profiles" / "brand"
    (brand_home / "cron").mkdir(parents=True)
    (brand_home / ".env").write_text("PROFILE_MARKER=brand\n", encoding="utf-8")
    (brand_home / "config.yaml").write_text("profile_marker: brand\n", encoding="utf-8")
    monkeypatch.setenv("BRAND_PROCESS_ONLY_SECRET", "brand-process-secret")

    seen = []
    stop = threading.Event()

    def tracking_tick(*_args, **kwargs):
        from agent.secret_scope import get_secret
        from cron.jobs import _current_cron_store
        from hermes_cli.config import read_user_config_raw

        seen.append(
            {
                "home": get_hermes_home(),
                "store": _current_cron_store().jobs_file,
                "config": read_user_config_raw(get_hermes_home() / "config.yaml")[
                    "profile_marker"
                ],
                "secret": get_secret("PROFILE_MARKER"),
                "process_secret": get_secret("BRAND_PROCESS_ONLY_SECRET"),
                "adapters": kwargs.get("adapters"),
            }
        )
        stop.set()
        return 0

    monkeypatch.setattr("cron.scheduler.tick", tracking_tick)
    monkeypatch.setattr("cron.jobs.record_ticker_heartbeat", lambda **_kwargs: None)

    InProcessCronScheduler().start(
        stop,
        interval=0,
        profile_homes=[("brand", brand_home)],
        profile_adapters={"brand": {"delivery_identity": "brand"}},
        owner_kind="gateway-dedicated",
        runtime_id="brand-gateway",
    )

    assert seen == [
        {
            "home": brand_home,
            "store": brand_home / "cron" / "jobs.json",
            "config": "brand",
            "secret": "brand",
            "process_secret": "brand-process-secret",
            "adapters": {"delivery_identity": "brand"},
        }
    ]
