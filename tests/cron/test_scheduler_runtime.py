"""Full-lifetime scheduler runtime lifecycle tests."""

from __future__ import annotations

import threading
import time
import json
import os
import signal
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from cron.scheduler_lease import SchedulerOwnershipLease
from cron.scheduler_provider import CronScheduler
from cron.scheduler_runtime import (
    OwnedSchedulerRuntime,
    SchedulerOwnershipPolicy,
    scheduler_runtime_is_eligible,
)


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


class _Provider(CronScheduler):
    def __init__(self, *, returns=False, blocks_stop=False):
        self.returns = returns
        self.blocks_stop = blocks_stop
        self.started = threading.Event()
        self.exited = threading.Event()
        self.release = threading.Event()

    @property
    def name(self):
        return "external"

    def start(self, stop_event, **_kwargs):
        self.started.set()
        if self.returns:
            return
        if self.blocks_stop:
            self.release.wait()
        else:
            stop_event.wait()
        self.exited.set()


def _run(runtime):
    stop = threading.Event()
    thread = threading.Thread(target=runtime.run, args=(stop,), daemon=True)
    thread.start()
    return stop, thread


@pytest.mark.parametrize(
    ("mode", "provider", "runtime", "gateway_running", "expected"),
    [
        ("gateway", "builtin", "gateway", False, True),
        ("gateway", "builtin", "desktop", False, False),
        ("desktop", "builtin", "desktop", True, True),
        ("desktop", "builtin", "gateway", False, False),
        ("auto", "builtin", "gateway", True, True),
        ("auto", "builtin", "desktop", False, True),
        ("auto", "builtin", "desktop", True, False),
        ("auto", "chronos", "gateway", False, True),
        ("auto", "chronos", "desktop", False, False),
    ],
)
def test_owner_eligibility_matrix(mode, provider, runtime, gateway_running, expected):
    assert (
        scheduler_runtime_is_eligible(
            SchedulerOwnershipPolicy(mode, provider),
            runtime=runtime,
            same_home_gateway_running=gateway_running,
        )
        is expected
    )


def test_external_start_return_keeps_lease(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "external")
    provider = _Provider(returns=True)
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    assert thread.is_alive()
    assert (
        SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        is None
    )
    stop.set()
    thread.join(2)
    assert not thread.is_alive()


def test_policy_handoff_has_no_overlap(tmp_path, monkeypatch):
    state = {"policy": SchedulerOwnershipPolicy("desktop", "builtin")}
    desktop_provider = _Provider()
    gateway_provider = _Provider()
    providers = iter([desktop_provider, gateway_provider])
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: state["policy"],
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: next(providers),
    )
    desktop = OwnedSchedulerRuntime("desktop", hermes_home=tmp_path, poll_interval=0.01)
    gateway = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    desktop_stop, desktop_thread = _run(desktop)
    gateway_stop, gateway_thread = _run(gateway)
    _wait(desktop_provider.started.is_set)
    assert not gateway_provider.started.is_set()
    state["policy"] = SchedulerOwnershipPolicy("gateway", "builtin")
    _wait(desktop_provider.exited.is_set)
    _wait(gateway_provider.started.is_set)
    assert desktop.active_provider is None
    desktop_stop.set()
    gateway_stop.set()
    desktop_thread.join(2)
    gateway_thread.join(2)


def test_gateway_recovers_after_initially_ineligible_policy(tmp_path, monkeypatch):
    state = {"policy": SchedulerOwnershipPolicy("desktop", "builtin")}
    provider = _Provider()
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: state["policy"],
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    time.sleep(0.05)
    assert not provider.started.is_set()
    state["policy"] = SchedulerOwnershipPolicy("gateway", "builtin")
    _wait(provider.started.is_set)
    stop.set()
    thread.join(2)


def test_blocking_shutdown_holds_lease_until_provider_exits(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "builtin")
    provider = _Provider(blocks_stop=True)
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime(
        "gateway", hermes_home=tmp_path, poll_interval=0.01, drain_timeout=0.02
    )
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    stop.set()
    time.sleep(0.05)
    assert thread.is_alive()
    assert (
        SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        is None
    )
    provider.release.set()
    thread.join(2)
    assert not thread.is_alive()


def test_drain_closes_admission_and_waits_for_external_fire(tmp_path, monkeypatch):
    from cron.scheduler_runtime import borrow_scheduler_provider

    policy = SchedulerOwnershipPolicy("gateway", "external")

    class Provider(_Provider):
        def __init__(self):
            super().__init__(returns=True)
            self.stopped = threading.Event()

        def stop(self):
            self.stopped.set()

    provider = Provider()
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)

    borrowed = threading.Event()
    release = threading.Event()

    def fire():
        with borrow_scheduler_provider(hermes_home=tmp_path) as active:
            assert active is provider
            borrowed.set()
            release.wait()

    fire_thread = threading.Thread(target=fire)
    fire_thread.start()
    _wait(borrowed.is_set)
    stop.set()
    time.sleep(0.05)
    assert thread.is_alive()
    assert not provider.stopped.is_set()
    with borrow_scheduler_provider(hermes_home=tmp_path) as active:
        assert active is None
    assert (
        SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        is None
    )
    release.set()
    fire_thread.join(2)
    thread.join(2)
    assert not thread.is_alive()
    assert provider.stopped.is_set()


def test_start_failure_drains_published_borrower_before_lease_release(
    tmp_path, monkeypatch
):
    from cron.scheduler_runtime import borrow_scheduler_provider

    policy = SchedulerOwnershipPolicy("gateway", "external")
    provider = _Provider(returns=True)
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    borrowed = threading.Event()
    release = threading.Event()
    borrower_thread = None
    original_start = threading.Thread.start

    def synthetic_start(thread):
        nonlocal borrower_thread
        if thread.name != "gateway-cron-provider":
            return original_start(thread)

        def borrow():
            with borrow_scheduler_provider(hermes_home=tmp_path) as active:
                assert active is provider
                borrowed.set()
                release.wait()

        borrower_thread = threading.Thread(target=borrow)
        original_start(borrower_thread)
        assert borrowed.wait(1)
        raise RuntimeError("synthetic Thread.start failure")

    monkeypatch.setattr(threading.Thread, "start", synthetic_start)
    result = {}

    def start_runtime():
        try:
            runtime._start_active(policy, tmp_path)
        except RuntimeError as exc:
            result["error"] = str(exc)

    starter = threading.Thread(target=start_runtime)
    original_start(starter)
    assert borrowed.wait(1)
    time.sleep(0.03)
    assert starter.is_alive()
    assert (
        SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        is None
    )
    release.set()
    starter.join(2)
    assert result == {"error": "synthetic Thread.start failure"}
    takeover = SchedulerOwnershipLease.try_acquire(
        hermes_home=tmp_path, owner="desktop", provider="builtin"
    )
    assert takeover is not None
    takeover.release()
    assert borrower_thread is not None
    borrower_thread.join(2)


def test_owner_detects_out_of_process_jobs_file_change(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "external")
    reconciled = threading.Event()

    class Provider(_Provider):
        def on_jobs_changed(self):
            reconciled.set()

    provider = Provider(returns=True)
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    jobs = tmp_path / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).write_text('[]', encoding='utf-8')",
            str(jobs),
        ],
        check=True,
    )
    _wait(reconciled.is_set)
    stop.set()
    thread.join(2)


def test_failed_reconciliation_remains_dirty_and_retries(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "external")
    succeeded = threading.Event()

    class Provider(_Provider):
        def __init__(self):
            super().__init__(returns=True)
            self.attempts = 0

        def on_jobs_changed(self):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("transient")
            succeeded.set()

    provider = Provider()
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    jobs = tmp_path / "cron" / "jobs.json"
    jobs.parent.mkdir(parents=True, exist_ok=True)
    jobs.write_text("[]", encoding="utf-8")
    _wait(succeeded.is_set)
    assert provider.attempts == 2
    stop.set()
    thread.join(2)


def test_jobs_signature_permission_error_retries_and_tears_down(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "external")
    provider = _Provider(returns=True)
    calls = {"count": 0}
    original = OwnedSchedulerRuntime._read_jobs_signature

    def transient(home):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("transient")
        return original(home)

    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    monkeypatch.setattr(
        OwnedSchedulerRuntime, "_read_jobs_signature", staticmethod(transient)
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    _wait(lambda: calls["count"] >= 2)
    assert thread.is_alive()
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    takeover = SchedulerOwnershipLease.try_acquire(
        hermes_home=tmp_path, owner="desktop", provider="builtin"
    )
    assert takeover is not None
    takeover.release()


def test_mixed_case_provider_policy_is_preserved():
    policy = __import__(
        "cron.scheduler_runtime", fromlist=["read_scheduler_ownership_policy_strict"]
    ).read_scheduler_ownership_policy_strict({
        "cron": {"scheduler_owner": "gateway", "provider": "MyProvider"}
    })
    assert policy == SchedulerOwnershipPolicy("gateway", "MyProvider")


def test_failed_startup_reconciliation_retries_with_backoff(tmp_path, monkeypatch):
    policy = SchedulerOwnershipPolicy("gateway", "external")
    started = threading.Event()
    providers = []

    class Provider(_Provider):
        def __init__(self, fail):
            super().__init__(returns=True)
            self.fail = fail

        def start(self, stop_event, **kwargs):
            providers.append(self)
            if self.fail:
                raise RuntimeError("transient startup reconcile")
            started.set()

    attempts = iter([Provider(True), Provider(False)])
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: next(attempts),
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.02)
    stop, thread = _run(runtime)
    _wait(started.is_set)
    assert len(providers) == 2
    stop.set()
    thread.join(2)


def test_gateway_presence_rejects_reused_pid_and_mismatched_identity(
    tmp_path, monkeypatch
):
    from cron.scheduler_runtime import exact_home_gateway_is_running

    path = tmp_path / "cron" / ".gateway-present.json"
    path.parent.mkdir(parents=True)
    record = {
        "pid": 1234,
        "start_time": 100,
        "kind": "gateway",
        "hermes_home": str(tmp_path),
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 101)
    assert exact_home_gateway_is_running(tmp_path) is False

    record["start_time"] = 101
    record["kind"] = "unrelated"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert exact_home_gateway_is_running(tmp_path) is False

    record["kind"] = "gateway"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert exact_home_gateway_is_running(tmp_path) is True


def test_gateway_presence_windows_path_never_uses_os_kill(tmp_path, monkeypatch):
    from cron.scheduler_runtime import exact_home_gateway_is_running

    path = tmp_path / "cron" / ".gateway-present.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({
            "pid": 4242,
            "start_time": 101,
            "kind": "gateway",
            "hermes_home": str(tmp_path),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 101)
    monkeypatch.setattr(
        "os.kill",
        lambda *_args: pytest.fail("Gateway presence must use the safe PID utility"),
    )
    assert exact_home_gateway_is_running(tmp_path) is True


def test_provider_stop_runs_under_exact_home(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    policy = SchedulerOwnershipPolicy("gateway", "external")

    class Provider(_Provider):
        def __init__(self):
            super().__init__(returns=True)
            self.stop_home = None

        def stop(self):
            self.stop_home = get_hermes_home()

    provider = Provider()
    monkeypatch.setattr(
        "cron.scheduler_runtime.read_scheduler_ownership_policy_strict",
        lambda **_kwargs: policy,
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda _name: provider,
    )
    runtime = OwnedSchedulerRuntime("gateway", hermes_home=tmp_path, poll_interval=0.01)
    stop, thread = _run(runtime)
    _wait(provider.started.is_set)
    stop.set()
    thread.join(2)
    assert provider.stop_home == tmp_path.resolve()


def test_user_provider_module_cache_isolated_per_profile_home(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.cron_providers import load_cron_scheduler

    def install(home, label):
        provider_dir = home / "plugins" / "custom"
        provider_dir.mkdir(parents=True)
        (provider_dir / "__init__.py").write_text(
            "from cron.scheduler_provider import CronScheduler\n"
            "class Provider(CronScheduler):\n"
            f"    name = {label!r}\n"
            "    def start(self, stop_event, **kwargs): pass\n",
            encoding="utf-8",
        )

    first = tmp_path / "first"
    second = tmp_path / "second"
    install(first, "first-provider")
    install(second, "second-provider")

    names = []
    for home in (first, second):
        token = set_hermes_home_override(home)
        try:
            names.append(load_cron_scheduler("custom").name)
        finally:
            reset_hermes_home_override(token)
    assert names == ["first-provider", "second-provider"]


def test_mixed_case_user_provider_loads_on_case_sensitive_path(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.cron_providers import load_cron_scheduler

    provider_dir = tmp_path / "plugins" / "MyProvider"
    provider_dir.mkdir(parents=True)
    (provider_dir / "__init__.py").write_text(
        "from cron.scheduler_provider import CronScheduler\n"
        "class Provider(CronScheduler):\n"
        "    name = 'MyProvider'\n"
        "    def start(self, stop_event, **kwargs): pass\n",
        encoding="utf-8",
    )
    token = set_hermes_home_override(tmp_path)
    try:
        provider = load_cron_scheduler("MyProvider")
    finally:
        reset_hermes_home_override(token)
    assert provider is not None
    assert provider.name == "MyProvider"


def test_unavailable_configured_provider_never_falls_back(monkeypatch):
    from cron.scheduler_provider import (
        InProcessCronScheduler,
        resolve_cron_scheduler_runtime_strict,
    )

    monkeypatch.setattr(
        "plugins.cron_providers.load_cron_scheduler", lambda _name: None
    )
    assert resolve_cron_scheduler_runtime_strict("chronos") is None
    assert isinstance(
        resolve_cron_scheduler_runtime_strict("builtin"),
        InProcessCronScheduler,
    )


def test_desktop_fire_callback_requires_exact_home_active_owner(tmp_path, monkeypatch):
    import hermes_cli.web_server as web_server

    home = tmp_path / "profile"
    monkeypatch.setattr(
        web_server, "_cron_profile_home", lambda _profile: ("worker", home)
    )
    borrowed_homes = []

    @contextmanager
    def borrow(*, hermes_home):
        borrowed_homes.append(hermes_home)
        yield None

    monkeypatch.setattr("cron.scheduler_runtime.borrow_scheduler_provider", borrow)
    assert web_server._fire_hosted_cron_job_for_profile("worker", "job-1") is None
    assert borrowed_homes == [home]


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires SIGKILL")
@pytest.mark.live_system_guard_bypass
def test_real_process_crash_allows_lease_takeover(tmp_path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time;"
                "from pathlib import Path;"
                "from cron.scheduler_lease import SchedulerOwnershipLease;"
                "lease=SchedulerOwnershipLease.try_acquire("
                "hermes_home=Path(sys.argv[1]),owner='gateway',provider='builtin');"
                "assert lease is not None;"
                "print('ready',flush=True);"
                "time.sleep(60)"
            ),
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(Path(__file__).parents[2]),
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "ready"
        assert (
            SchedulerOwnershipLease.try_acquire(
                hermes_home=tmp_path, owner="desktop", provider="builtin"
            )
            is None
        )
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=3)
        takeover = SchedulerOwnershipLease.try_acquire(
            hermes_home=tmp_path, owner="desktop", provider="builtin"
        )
        assert takeover is not None
        takeover.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)


def test_hosted_external_fire_resolves_strict_exact_home(tmp_path, monkeypatch):
    import hermes_cli.web_server as web_server
    from hermes_constants import get_hermes_home

    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "cron:\n  scheduler_owner: auto\n  provider: external\n",
        encoding="utf-8",
    )
    seen = {}

    class Provider:
        def fire_due(self, job_id, *, adapters=None, loop=None):
            seen["job_id"] = job_id
            seen["home"] = get_hermes_home()
            return True

    monkeypatch.setattr(
        web_server, "_cron_profile_home", lambda _profile: ("worker", home)
    )
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler_runtime_strict",
        lambda name: Provider() if name == "external" else None,
    )
    assert web_server._fire_hosted_cron_job_for_profile("worker", "job-1") is True
    assert seen == {"job_id": "job-1", "home": home.resolve()}


@pytest.mark.asyncio
async def test_desktop_shutdown_wait_is_bounded_and_nonblocking():
    import asyncio
    import hermes_cli.web_server as web_server

    class AliveThread:
        def is_alive(self):
            return True

    stop = threading.Event()
    heartbeat = asyncio.Event()

    async def beat():
        await asyncio.sleep(0.01)
        heartbeat.set()

    task = asyncio.create_task(beat())
    started = time.monotonic()
    stopped = await web_server._stop_desktop_cron_scheduler(
        object(), stop, AliveThread(), timeout=0.05
    )
    await task
    assert stopped is False
    assert stop.is_set() and heartbeat.is_set()
    assert time.monotonic() - started < 0.5
