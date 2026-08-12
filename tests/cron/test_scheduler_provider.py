"""Characterization tests for the cron trigger before/after the provider refactor.

These lock the CURRENT in-process-ticker contract (Phase 0 of the pluggable
CronScheduler plan, .hermes/plans/cron-scheduler-provider-interface.md). They
must pass unchanged on `main` now, and after every subsequent phase of the
refactor — they are the regression harness that proves the built-in firing
behavior is byte-for-byte preserved when the ticker is moved behind the
CronScheduler provider interface.

No production code is exercised beyond the two ticker entry points:
  - gateway/run.py::_start_cron_ticker        (production gateway ticker)
  - hermes_cli/web_server.py::_start_desktop_cron_ticker  (desktop fallback)

Both call `cron.scheduler.tick(...)` on a loop and exit when their stop_event
is set. We patch `cron.scheduler.tick` (both tickers import it locally as
`cron_tick`, so the module-attribute patch is observed) and assert the loop
drives it and stops promptly.
"""
import threading
import time
from unittest.mock import patch


def _wait_until(predicate, timeout=10.0, interval=0.005):
    """Block until ``predicate()`` is truthy or ``timeout`` elapses.

    Returns the predicate's final value. Used instead of a fixed
    ``time.sleep`` before asserting that a background ticker thread has called
    tick()/heartbeat() at least N times — under loaded CI the worker thread may
    not be scheduled within a short fixed sleep, which made these tests flake
    (``assert 0 >= 1`` / ``provider never called tick()``).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def test_ticker_calls_tick_at_least_once_then_stops():
    """The gateway in-process ticker loop calls cron.scheduler.tick repeatedly
    and exits promptly once the stop_event is set."""
    from gateway.run import _start_cron_ticker

    calls = []
    stop = threading.Event()

    def fake_tick(*args, **kwargs):
        calls.append(kwargs)
        return 0

    with patch("cron.scheduler.tick", side_effect=fake_tick):
        # interval=0 keeps the loop tight; stop after the first observed tick.
        t = threading.Thread(
            target=_start_cron_ticker,
            args=(stop,),
            kwargs={"interval": 0},
            daemon=True,
        )
        t.start()
        assert _wait_until(lambda: len(calls) >= 1), "ticker never called tick()"
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "ticker did not exit after stop_event was set"
    assert len(calls) >= 1, "ticker never called tick()"
    # Contract: the ticker invokes tick with sync=False (fire-and-forget from
    # the background thread, never the synchronous CLI path).
    assert calls[0].get("sync") is False


def test_desktop_ticker_calls_tick_then_stops():
    """The desktop dashboard ticker loop calls cron.scheduler.tick and exits
    once the stop_event is set. Desktop has no live adapters, so it ticks with
    no adapters/loop."""
    from hermes_cli.web_server import _start_desktop_cron_ticker

    calls = []
    stop = threading.Event()

    def fake_tick(*args, **kwargs):
        calls.append(kwargs)
        return 0

    with (
        patch("cron.jobs.gateway_ticker_lease_is_fresh", return_value=False),
        patch("cron.scheduler.tick", side_effect=fake_tick),
    ):
        t = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(stop,),
            kwargs={"interval": 0},
            daemon=True,
        )
        t.start()
        assert _wait_until(lambda: len(calls) >= 1), "desktop ticker never called tick()"
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "desktop ticker did not exit after stop_event was set"
    assert len(calls) >= 1, "desktop ticker never called tick()"
    assert calls[0].get("sync") is False


def test_desktop_ticker_yields_while_gateway_is_running():
    """The desktop fallback must not dispatch alongside a real gateway.

    The tick file lock only serializes the short due-job scan.  Agent jobs run
    asynchronously after that lock is released, so two provider loops can
    otherwise execute the same recurring job at the same time.
    """
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_cli.web_server import _start_desktop_cron_ticker

    calls = []
    gateway_checked = threading.Event()
    gateway_ready = threading.Event()
    gateway_ready.set()
    stop = threading.Event()

    def fake_gateway_ready():
        gateway_checked.set()
        return gateway_ready.is_set()

    with (
        patch(
            "cron.jobs.gateway_ticker_lease_is_fresh",
            side_effect=fake_gateway_ready,
        ),
        patch.object(
            InProcessCronScheduler,
            "recover_interrupted",
            return_value=0,
        ) as recover_interrupted,
        patch(
            "cron.scheduler.tick",
            side_effect=lambda *args, **kwargs: calls.append(kwargs) or 0,
        ),
    ):
        t = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(stop,),
            kwargs={"interval": 0.01},
            daemon=True,
        )
        t.start()
        assert gateway_checked.wait(timeout=5), "desktop ticker never checked gateway"
        assert calls == [], "desktop fallback dispatched before entering standby"
        assert not recover_interrupted.called, (
            "desktop fallback reconciled while gateway owned dispatch"
        )
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "desktop ticker did not exit after stop_event was set"
    assert calls == [], "desktop fallback dispatched while gateway was running"
    assert not recover_interrupted.called, (
        "desktop fallback reconciled another live scheduler's executions"
    )


def test_desktop_ticker_takes_over_after_gateway_exits():
    """The standby remains a fallback when the real gateway goes away."""
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_cli.web_server import _start_desktop_cron_ticker

    calls = []
    gateway_checked = threading.Event()
    gateway_ready = threading.Event()
    gateway_ready.set()
    stop = threading.Event()

    def fake_gateway_ready():
        gateway_checked.set()
        return gateway_ready.is_set()

    with (
        patch(
            "cron.jobs.gateway_ticker_lease_is_fresh",
            side_effect=fake_gateway_ready,
        ),
        patch.object(
            InProcessCronScheduler,
            "recover_interrupted",
            return_value=0,
        ) as recover_interrupted,
        patch(
            "cron.scheduler.tick",
            side_effect=lambda *args, **kwargs: calls.append(kwargs) or 0,
        ),
    ):
        t = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(stop,),
            kwargs={"interval": 0.01},
            daemon=True,
        )
        t.start()
        assert gateway_checked.wait(timeout=5), "desktop ticker never checked gateway"
        assert calls == [], "desktop fallback dispatched while gateway was ready"
        assert not recover_interrupted.called, (
            "desktop fallback reconciled while gateway was ready"
        )
        gateway_ready.clear()
        assert _wait_until(lambda: len(calls) >= 1), (
            "desktop ticker did not take over after gateway exit"
        )
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "desktop ticker did not exit after stop_event was set"
    recover_interrupted.assert_called_once_with()
    assert calls[0].get("sync") is False


def test_desktop_ticker_rechecks_gateway_before_recovery():
    """A gateway becoming ready at handoff must fence desktop recovery.

    The desktop performs an initial readiness probe before starting the
    provider.  The provider must probe again before reconciling executions;
    otherwise a gateway that becomes ready between those two operations can
    have its live execution marked interrupted by the desktop.
    """
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_cli.web_server import _start_desktop_cron_ticker

    probes = 0
    provider_probe = threading.Event()
    stop = threading.Event()

    def gateway_becomes_ready():
        nonlocal probes
        probes += 1
        if probes <= 2:
            return False
        provider_probe.set()
        return True

    with (
        patch(
            "cron.jobs.gateway_ticker_lease_is_fresh",
            side_effect=gateway_becomes_ready,
        ),
        patch.object(
            InProcessCronScheduler,
            "recover_interrupted",
            return_value=0,
        ) as recover_interrupted,
        patch("cron.scheduler.tick") as cron_tick,
    ):
        t = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(stop,),
            kwargs={"interval": 0.01},
            daemon=True,
        )
        t.start()
        assert provider_probe.wait(timeout=5), "provider never rechecked gateway lease"
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "desktop ticker did not exit after stop_event was set"
    assert not recover_interrupted.called
    assert not cron_tick.called


# ── Phase 1: CronScheduler ABC + InProcessCronScheduler ──────────────────────


def test_cronscheduler_is_abstract():
    """name + start are abstract — the bare ABC can't be instantiated."""
    import pytest
    from cron.scheduler_provider import CronScheduler

    with pytest.raises(TypeError):
        CronScheduler()


def test_abc_growth_stays_additive():
    """The provider interface stays source-compatible with existing plugins.

    ``start`` must be the only required implementation hook: future optional
    behavior belongs in non-abstract default methods so custom plugins do not
    break on import after an upgrade.
    """
    from cron.scheduler_provider import CronScheduler

    abstract = set(getattr(CronScheduler, "__abstractmethods__", set()))
    assert abstract == {"name", "start"}, (
        f"CronScheduler abstractmethods changed to {abstract}; growth must be "
        "additive (optional methods with defaults), not new abstract methods."
    )


def test_inprocess_provider_ticks_and_stops():
    """The built-in provider drives cron.scheduler.tick(sync=False) on a loop
    and exits promptly when stop_event is set — same contract as the raw
    ticker characterized above."""
    from cron.scheduler_provider import InProcessCronScheduler

    calls = []
    stop = threading.Event()
    prov = InProcessCronScheduler()
    assert prov.name == "builtin"

    with patch("cron.scheduler.tick", side_effect=lambda *a, **k: calls.append(k) or 0):
        t = threading.Thread(
            target=prov.start, args=(stop,), kwargs={"interval": 0}, daemon=True
        )
        t.start()
        # Wait for the loop to actually call tick() at least once rather than
        # sleeping a fixed window — under loaded CI the worker thread may not be
        # scheduled within a short sleep, which made this flake (assert 0 >= 1).
        assert _wait_until(lambda: len(calls) >= 1), "provider never called tick()"
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive(), "provider did not exit after stop_event was set"
    assert len(calls) >= 1, "provider never called tick()"
    assert calls[0].get("sync") is False


# ── Phase 2: config key, discovery, resolver ─────────────────────────────────


def test_default_config_cron_provider_is_empty():
    """The new cron.provider key defaults to empty (= built-in)."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["cron"]["provider"] == ""


def test_discover_cron_schedulers_returns_list():
    """Discovery returns bundled non-default providers.

    The built-in is core, not discovered here.
    """
    from plugins.cron_providers import discover_cron_schedulers

    result = discover_cron_schedulers()
    assert isinstance(result, list)
    assert any(name == "chronos" for name, _desc, _available in result)


def test_load_unknown_cron_scheduler_returns_none():
    from plugins.cron_providers import load_cron_scheduler

    assert load_cron_scheduler("does-not-exist-xyz") is None


def test_cron_provider_package_does_not_shadow_core_cron_package(monkeypatch):
    """Putting plugins/ first on sys.path must not hide the core cron package."""
    from importlib.machinery import PathFinder
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]

    monkeypatch.syspath_prepend(str(repo_root))
    monkeypatch.syspath_prepend(str(repo_root / "plugins"))

    cron_spec = PathFinder.find_spec("cron")
    assert cron_spec is not None
    assert Path(cron_spec.origin).resolve() == repo_root / "cron" / "__init__.py"

    jobs_spec = PathFinder.find_spec("cron.jobs", [str(repo_root / "cron")])
    assert jobs_spec is not None
    assert Path(jobs_spec.origin).resolve() == repo_root / "cron" / "jobs.py"


def test_resolve_defaults_to_builtin(monkeypatch):
    """Empty cron.provider → built-in."""
    import hermes_cli.config as cfg
    from cron import scheduler_provider as sp

    monkeypatch.setattr(cfg, "load_config", lambda: {"cron": {"provider": ""}})
    prov = sp.resolve_cron_scheduler()
    assert prov.name == "builtin"


# ── Phase 4B: additive hooks (on_jobs_changed / fire_due / reconcile) ────────


def test_hooks_did_not_change_required_surface():
    """The additive hooks must NOT become abstractmethods — the Phase-1 guard
    still holds (required surface is exactly name + start)."""
    from cron.scheduler_provider import CronScheduler

    assert set(CronScheduler.__abstractmethods__) == {"name", "start"}


def test_builtin_inherits_hook_defaults():
    """The built-in inherits no-op defaults for the new hooks (it never needs
    to override them)."""
    from cron.scheduler_provider import InProcessCronScheduler

    p = InProcessCronScheduler()
    assert p.on_jobs_changed() is None
    assert p.reconcile() is None
    # built-in does not override fire_due; it simply isn't called for built-in.
    assert hasattr(p, "fire_due")


def test_fire_due_default_claims_then_runs(monkeypatch):
    """The default fire_due claims via the store CAS, fetches the job, and runs
    it through the shared run_one_job body."""
    import cron.jobs as jobs
    import cron.scheduler as sched
    from cron.scheduler_provider import InProcessCronScheduler

    ran = []
    monkeypatch.setattr(
        jobs,
        "claim_job_for_fire_attempt",
        lambda jid: "attempt",
        raising=False,
    )
    monkeypatch.setattr(
        jobs,
        "get_job",
        lambda jid: {
            "id": jid,
            "name": "t",
            "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt"},
        },
    )
    monkeypatch.setattr(sched, "run_one_job", lambda job, **kw: ran.append(job["id"]) or True)

    assert InProcessCronScheduler().fire_due("j1") is True
    assert ran == ["j1"]


def test_fire_due_does_not_adopt_replacement_attempt(monkeypatch):
    import cron.jobs as jobs
    import cron.scheduler as sched
    from cron.scheduler_provider import InProcessCronScheduler

    monkeypatch.setattr(
        jobs, "claim_job_for_fire_attempt", lambda _jid: "original"
    )
    monkeypatch.setattr(
        jobs,
        "get_job",
        lambda jid: {
            "id": jid,
            "run_claim": {"at": "2026-01-01T00:00:01+00:00", "by": "successor"},
        },
    )
    ran = []
    monkeypatch.setattr(
        sched, "run_one_job", lambda *_a, **_kw: ran.append(True)
    )

    assert InProcessCronScheduler().fire_due("j1") is False
    assert ran == []


def test_fire_due_releases_exact_claim_when_refetch_raises(monkeypatch):
    """An exception before handoff must not strand the acquired attempt."""
    import cron.jobs as jobs
    from cron.scheduler_provider import InProcessCronScheduler

    released = []
    monkeypatch.setattr(
        jobs, "claim_job_for_fire_attempt", lambda _jid: "attempt-owner"
    )
    monkeypatch.setattr(
        jobs, "get_job", lambda _jid: (_ for _ in ()).throw(OSError("read failed"))
    )
    monkeypatch.setattr(
        jobs,
        "release_run_claim",
        lambda jid, *, expected_owner: released.append((jid, expected_owner)) or True,
    )

    assert InProcessCronScheduler().fire_due("j1") is False
    assert released == [("j1", "attempt-owner")]


def test_fire_due_releases_exact_claim_when_execution_create_raises(monkeypatch):
    """Execution-ledger failure before worker handoff must release the token."""
    import cron.executions as executions
    import cron.jobs as jobs
    from cron.scheduler_provider import InProcessCronScheduler

    released = []
    monkeypatch.setattr(
        jobs, "claim_job_for_fire_attempt", lambda _jid: "attempt-owner"
    )
    monkeypatch.setattr(
        jobs,
        "get_job",
        lambda jid: {
            "id": jid,
            "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-owner"},
        },
    )
    monkeypatch.setattr(
        jobs,
        "release_run_claim",
        lambda jid, *, expected_owner: released.append((jid, expected_owner)) or True,
    )
    monkeypatch.setattr(
        executions,
        "create_execution",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("ledger unavailable")),
    )

    assert InProcessCronScheduler().fire_due("j1") is False
    assert released == [("j1", "attempt-owner")]


# ── F2a: ticker liveness — survival, heartbeat, honest status (#32612, #32895) ──


def test_failing_tick_records_liveness_but_not_success():
    """A tick that raises bumps the liveness heartbeat but NOT the success
    marker — so status can distinguish 'alive but failing' from 'firing'."""
    from cron.scheduler_provider import InProcessCronScheduler

    beats = []
    stop = threading.Event()
    prov = InProcessCronScheduler()
    with patch("cron.scheduler.tick", side_effect=RuntimeError("every tick fails")), \
         patch("cron.jobs.record_ticker_heartbeat",
               side_effect=lambda success=False: beats.append(success)):
        t = threading.Thread(target=prov.start, args=(stop,), kwargs={"interval": 0}, daemon=True)
        t.start()
        # Wait for the pre-loop beat + at least one post-tick beat (was flaky
        # with a fixed 0.2s sleep under loaded CI).
        assert _wait_until(lambda: len(beats) >= 2), "ticker did not record heartbeats"
        stop.set()
        t.join(timeout=5)

    # every post-tick beat must be success=False (ticks always failed)
    assert len(beats) >= 2
    assert all(b is False for b in beats), "a failing tick wrongly bumped the success marker"


def test_heartbeat_roundtrip_and_age(tmp_path, monkeypatch):
    """record_ticker_heartbeat writes fresh timestamps atomically; the age
    getters read them back as small positive ages."""
    import cron.jobs as jobs

    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(jobs, "TICKER_HEARTBEAT_FILE", cron_dir / "ticker_heartbeat")
    monkeypatch.setattr(jobs, "TICKER_SUCCESS_FILE", cron_dir / "ticker_last_success")

    # No files yet -> unknown (None), NOT "dead"
    assert jobs.get_ticker_heartbeat_age() is None
    assert jobs.get_ticker_success_age() is None

    # liveness-only: heartbeat set, success still unknown
    jobs.record_ticker_heartbeat(success=False)
    hb = jobs.get_ticker_heartbeat_age()
    assert hb is not None and 0.0 <= hb < 5.0
    assert jobs.get_ticker_success_age() is None

    # success: both set
    jobs.record_ticker_heartbeat(success=True)
    ok = jobs.get_ticker_success_age()
    assert ok is not None and 0.0 <= ok < 5.0


def test_gateway_ticker_lease_is_profile_scoped_and_owner_local(tmp_path):
    """Gateway readiness is per profile and one owner cannot clear another."""
    import cron.jobs as jobs

    default_home = tmp_path / "default"
    ops_home = tmp_path / "home-ops"

    with jobs.use_cron_store(default_home):
        assert not jobs.gateway_ticker_lease_is_fresh()
        jobs.record_gateway_ticker_lease("123-owner-a")
        jobs.record_gateway_ticker_lease("456-owner-b")
        assert jobs.gateway_ticker_lease_is_fresh()

    with jobs.use_cron_store(ops_home):
        assert not jobs.gateway_ticker_lease_is_fresh(), (
            "a gateway serving another profile must not fence this profile"
        )

    with jobs.use_cron_store(default_home):
        jobs.clear_gateway_ticker_lease("123-owner-a")
        assert jobs.gateway_ticker_lease_is_fresh(), (
            "an exiting predecessor removed its successor's readiness lease"
        )
        jobs.clear_gateway_ticker_lease("456-owner-b")
        assert not jobs.gateway_ticker_lease_is_fresh()


def test_gateway_ticker_lease_expires_and_rejects_unsafe_owner(tmp_path, monkeypatch):
    """A crashed gateway's lease expires and owner IDs cannot escape cron/."""
    import pytest
    import cron.jobs as jobs

    with jobs.use_cron_store(tmp_path):
        written_at = time.time()
        jobs.record_gateway_ticker_lease("123-owner")
        assert jobs.gateway_ticker_lease_is_fresh()

        monkeypatch.setattr(
            jobs.time,
            "time",
            lambda: written_at + jobs.GATEWAY_TICKER_LEASE_STALE_SECONDS + 1,
        )
        assert not jobs.gateway_ticker_lease_is_fresh()

        with pytest.raises(ValueError):
            jobs.record_gateway_ticker_lease("../outside")
        with pytest.raises(ValueError):
            jobs.clear_gateway_ticker_lease("../outside")


def test_gateway_ticker_lease_rejects_future_timestamp(tmp_path, monkeypatch):
    """A clock-skewed marker cannot extend ownership by another full TTL."""
    import cron.jobs as jobs

    now = time.time()
    owner = "123-future-owner"
    with jobs.use_cron_store(tmp_path):
        jobs.record_gateway_ticker_lease(owner)
        jobs._gateway_ticker_lease_path(owner).write_text(
            str(now + jobs._GATEWAY_TICKER_LEASE_FUTURE_SKEW_SECONDS + 1)
        )
        monkeypatch.setattr(jobs.time, "time", lambda: now)

        assert not jobs.gateway_ticker_lease_is_fresh()


def test_stale_gateway_lease_is_pruned_only_after_exact_owner_death(
    tmp_path, monkeypatch
):
    """Crash remnants stay while identity is live/unknown, then are reclaimed."""
    import cron.jobs as jobs

    owner = f"p123-s456-{'a' * 32}"
    with jobs.use_cron_store(tmp_path):
        jobs.record_gateway_ticker_lease(owner)
        lease_path = jobs._gateway_ticker_lease_path(owner)
        written_at = time.time()
        monkeypatch.setattr(
            jobs.time,
            "time",
            lambda: written_at + jobs.GATEWAY_TICKER_LEASE_STALE_SECONDS + 1,
        )

        monkeypatch.setattr(
            jobs,
            "_gateway_ticker_owner_is_definitely_dead",
            lambda _owner: False,
        )
        assert not jobs.gateway_ticker_lease_is_fresh()
        assert lease_path.exists(), "probe deleted a lease without proof of death"

        monkeypatch.setattr(
            jobs,
            "_gateway_ticker_owner_is_definitely_dead",
            lambda _owner: True,
        )
        assert not jobs.gateway_ticker_lease_is_fresh()
        assert not lease_path.exists(), "dead owner's stale lease was never reclaimed"


def test_gateway_lease_owner_death_uses_pid_and_start_fingerprint(monkeypatch):
    import cron.jobs as jobs
    import gateway.status as gateway_status

    owner = f"p123-s456-{'a' * 32}"
    monkeypatch.setattr(gateway_status, "_pid_exists", lambda _pid: True)
    monkeypatch.setattr(
        gateway_status, "get_stable_process_start_time", lambda _pid: 456
    )
    assert not jobs._gateway_ticker_owner_is_definitely_dead(owner)

    monkeypatch.setattr(
        gateway_status, "get_stable_process_start_time", lambda _pid: 789
    )
    assert jobs._gateway_ticker_owner_is_definitely_dead(owner)

    monkeypatch.setattr(gateway_status, "_pid_exists", lambda _pid: False)
    assert jobs._gateway_ticker_owner_is_definitely_dead(owner)


def test_gateway_provider_owns_lease_for_its_lifecycle():
    """Readiness is published after recovery, then cleaned up on shutdown."""
    from cron.scheduler_provider import InProcessCronScheduler

    events = []
    stop = threading.Event()
    provider = InProcessCronScheduler()

    def stop_after_tick(**_kwargs):
        events.append(("tick", None))
        stop.set()
        return 0

    with (
        patch(
            "cron.jobs.record_gateway_ticker_lease",
            side_effect=lambda owner: events.append(("lease", owner)),
        ),
        patch(
            "cron.jobs.clear_gateway_ticker_lease",
            side_effect=lambda owner: events.append(("clear", owner)),
        ),
        patch.object(
            provider,
            "recover_interrupted",
            side_effect=lambda: events.append(("recover", None)) or 0,
        ),
        patch(
            "cron.jobs.record_ticker_heartbeat",
            side_effect=lambda **_kwargs: events.append(("heartbeat", None)),
        ),
        patch("cron.scheduler.tick", side_effect=stop_after_tick),
    ):
        provider.start(stop, interval=0, gateway_owner_id="123-owner")

    assert events == [
        ("recover", None),
        ("heartbeat", None),
        ("lease", "123-owner"),
        ("tick", None),
        ("heartbeat", None),
        ("clear", "123-owner"),
    ]


def test_gateway_provider_retries_transient_lease_write_before_dispatch():
    """A lease fsync failure skips that cycle; it does not kill the ticker."""
    from cron.scheduler_provider import InProcessCronScheduler

    attempts = []
    ticks = []
    stop = threading.Event()
    provider = InProcessCronScheduler()

    def flaky_publish(owner):
        attempts.append(owner)
        if len(attempts) == 1:
            raise OSError("transient fsync failure")

    def successful_tick(**_kwargs):
        ticks.append(True)
        stop.set()
        return 0

    with (
        patch("cron.jobs.record_gateway_ticker_lease", side_effect=flaky_publish),
        patch("cron.jobs.clear_gateway_ticker_lease"),
        patch("cron.jobs.record_ticker_heartbeat"),
        patch("cron.jobs.record_ticker_error"),
        patch("cron.jobs.clear_ticker_error"),
        patch.object(provider, "recover_interrupted", return_value=0),
        patch("cron.scheduler.tick", side_effect=successful_tick),
    ):
        provider.start(stop, interval=0, gateway_owner_id="123-owner")

    assert attempts == ["123-owner", "123-owner"]
    assert ticks == [True], "provider dispatched before it could publish readiness"


def test_gateway_provider_survives_errors_while_reporting_lease_failure():
    """Secondary cleanup/diagnostic failures must not kill the ticker loop."""
    from cron.scheduler_provider import InProcessCronScheduler

    attempts = []
    ticks = []
    stop = threading.Event()
    provider = InProcessCronScheduler()

    def flaky_publish(_owner):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("lease write failed")

    def successful_tick(**_kwargs):
        ticks.append(True)
        stop.set()
        return 0

    with (
        patch("cron.jobs.record_gateway_ticker_lease", side_effect=flaky_publish),
        patch("cron.jobs.clear_gateway_ticker_lease", side_effect=OSError("cleanup failed")),
        patch("cron.jobs.record_ticker_error", side_effect=OSError("diagnostic failed")),
        patch("cron.jobs.record_ticker_heartbeat"),
        patch("cron.jobs.clear_ticker_error"),
        patch.object(provider, "recover_interrupted", return_value=0),
        patch("cron.scheduler.tick", side_effect=successful_tick),
    ):
        provider.start(stop, interval=0, gateway_owner_id="123-owner")

    assert len(attempts) == 2
    assert ticks == [True]


def test_multiplex_gateway_lease_covers_every_served_profile(tmp_path):
    """A multiplex gateway advertises readiness in every profile it ticks."""
    import cron.jobs as jobs
    from cron.scheduler_provider import InProcessCronScheduler

    profile_homes = [
        ("default", tmp_path / "default"),
        ("home-ops", tmp_path / "home-ops"),
    ]
    for _name, home in profile_homes:
        (home / "cron").mkdir(parents=True)

    stop = threading.Event()
    provider = InProcessCronScheduler()

    def every_profile_is_ready():
        for _name, home in profile_homes:
            with jobs.use_cron_store(home):
                if not jobs.gateway_ticker_lease_is_fresh():
                    return False
        return True

    with (
        patch.object(provider, "recover_interrupted", return_value=0),
        patch("cron.scheduler.tick", return_value=0),
    ):
        ticker = threading.Thread(
            target=provider.start,
            args=(stop,),
            kwargs={
                "interval": 0.01,
                "profile_homes": profile_homes,
                "gateway_owner_id": "123-multiplex",
            },
            daemon=True,
        )
        ticker.start()
        assert _wait_until(every_profile_is_ready), (
            "multiplex gateway did not publish all profile leases"
        )
        stop.set()
        ticker.join(timeout=5)

    assert not ticker.is_alive()
    for _name, home in profile_homes:
        with jobs.use_cron_store(home):
            assert not jobs.gateway_ticker_lease_is_fresh(), (
                f"gateway lease for {home} survived clean shutdown"
            )


def test_multiplex_gateway_waits_for_all_profile_initialization(tmp_path):
    """No profile advertises readiness while a later profile is still recovering."""
    import cron.jobs as jobs
    from cron.scheduler_provider import InProcessCronScheduler

    profile_homes = [
        ("default", tmp_path / "default"),
        ("home-ops", tmp_path / "home-ops"),
    ]
    for _name, home in profile_homes:
        (home / "cron").mkdir(parents=True)

    second_recovery_started = threading.Event()
    release_recovery = threading.Event()
    stop = threading.Event()
    recoveries = 0
    provider = InProcessCronScheduler()

    def recover_profile():
        nonlocal recoveries
        recoveries += 1
        if recoveries == 2:
            second_recovery_started.set()
            assert release_recovery.wait(timeout=5)
        return 0

    with (
        patch.object(provider, "recover_interrupted", side_effect=recover_profile),
        patch("cron.scheduler.tick", return_value=0),
    ):
        ticker = threading.Thread(
            target=provider.start,
            args=(stop,),
            kwargs={
                "interval": 0.01,
                "profile_homes": profile_homes,
                "gateway_owner_id": "123-multiplex",
            },
            daemon=True,
        )
        ticker.start()
        assert second_recovery_started.wait(timeout=5)
        with jobs.use_cron_store(profile_homes[0][1]):
            assert not jobs.gateway_ticker_lease_is_fresh(), (
                "first profile advertised ready before all profiles initialized"
            )
        release_recovery.set()
        assert _wait_until(
            lambda: all(
                _profile_gateway_lease_is_fresh(jobs, home)
                for _name, home in profile_homes
            )
        )
        stop.set()
        ticker.join(timeout=5)

    assert not ticker.is_alive()


def _profile_gateway_lease_is_fresh(jobs, home):
    with jobs.use_cron_store(home):
        return jobs.gateway_ticker_lease_is_fresh()


def test_multiplex_gateway_retries_partial_lease_publish_before_any_tick(tmp_path):
    """One profile's transient write failure must not dispatch a partial cycle."""
    import cron.jobs as jobs
    from cron.scheduler_provider import InProcessCronScheduler

    profile_homes = [
        ("default", tmp_path / "default"),
        ("home-ops", tmp_path / "home-ops"),
    ]
    for _name, home in profile_homes:
        (home / "cron").mkdir(parents=True)

    publishes = []
    ticks = []
    stop = threading.Event()
    provider = InProcessCronScheduler()

    def flaky_publish(_owner):
        publishes.append(jobs._current_cron_store().cron_dir.parent.name)
        if len(publishes) == 2:
            raise OSError("transient profile fsync failure")

    def track_tick(**_kwargs):
        ticks.append(jobs._current_cron_store().cron_dir.parent.name)
        if len(ticks) == len(profile_homes):
            stop.set()
        return 0

    with (
        patch.object(provider, "recover_interrupted", return_value=0),
        patch("cron.jobs.record_gateway_ticker_lease", side_effect=flaky_publish),
        patch("cron.jobs.clear_gateway_ticker_lease"),
        patch("cron.jobs.record_ticker_heartbeat"),
        patch("cron.jobs.record_ticker_error"),
        patch("cron.jobs.clear_ticker_error"),
        patch("cron.scheduler.tick", side_effect=track_tick),
    ):
        provider.start(
            stop,
            interval=0,
            profile_homes=profile_homes,
            gateway_owner_id="123-multiplex",
        )

    assert publishes == ["default", "home-ops", "default", "home-ops"]
    assert ticks == ["default", "home-ops"]


# ── F8: runtime backstop — never resolve a stored pair that exfiltrates a key ──


class TestGuardJobCredentialExfil:
    """run_job() must fail closed before provider resolution when a job's stored
    provider/base_url pair would ship a named provider's stored credential to an
    off-host endpoint — covering jobs persisted before the create/update guard
    or written directly to the store (F8 stored-job path; CWE-200/CWE-522)."""

    def test_named_registry_provider_offhost_is_blocked(self):
        import pytest
        from cron.scheduler import _guard_job_credential_exfil

        job = {"id": "j1", "provider": "anthropic",
               "base_url": "https://evil.example/v1"}
        with pytest.raises(RuntimeError) as exc:
            _guard_job_credential_exfil(job)
        assert "blocked for safety" in str(exc.value)


    def test_bare_custom_is_allowed(self):
        from cron.scheduler import _guard_job_credential_exfil

        job = {"id": "j4", "provider": "custom",
               "base_url": "https://anything.example/v1"}
        assert _guard_job_credential_exfil(job) is None

    def test_no_base_url_is_allowed(self):
        from cron.scheduler import _guard_job_credential_exfil

        assert _guard_job_credential_exfil({"id": "j5", "provider": "anthropic"}) is None
        assert _guard_job_credential_exfil({"id": "j6"}) is None

    def test_validator_exception_with_base_url_fails_closed(self, monkeypatch):
        # If the validator/import unexpectedly raises, this last-resort backstop
        # must NOT allow a base_url-bearing job through to provider resolution
        # (it cannot prove the stored pair is safe). Regression for the
        # fail-open `except Exception: err = None` path.
        import pytest
        import tools.cronjob_tools as ct
        from cron.scheduler import _guard_job_credential_exfil

        def _boom(provider, base_url):
            raise RuntimeError("validator blew up")

        monkeypatch.setattr(ct, "_validate_cron_base_url", _boom)
        job = {"id": "j7", "provider": "custom:legit",
               "base_url": "https://evil.example/v1"}
        with pytest.raises(RuntimeError) as exc:
            _guard_job_credential_exfil(job)
        assert "blocked for safety" in str(exc.value)


# ── Multiplex profiles: cron per secondary profile (issue #69377) ─────────


def test_multiplex_ticker_ticks_each_profile_once(tmp_path, monkeypatch):
    """The multiplex cron scheduler calls tick() once per profile home,
    scoped via use_cron_store, so secondary-profile jobs actually fire
    instead of languishing in an unticked store."""
    from cron.scheduler_provider import InProcessCronScheduler

    # Set up two profile directories.
    p1 = tmp_path / "default"
    p2 = tmp_path / "home-ops"
    for d in (p1, p2):
        (d / "cron").mkdir(parents=True)

    profile_homes = [("default", p1), ("home-ops", p2)]

    # Count tick() calls — should be called once per profile per iteration.
    tick_count: list[int] = []

    def _tracking_tick(*args, **kwargs):
        tick_count.append(1)
        return 0

    stop = threading.Event()
    prov = InProcessCronScheduler()

    with patch("cron.scheduler.tick", side_effect=_tracking_tick), \
         patch("cron.jobs.record_ticker_heartbeat", lambda **kw: None):
        t = threading.Thread(
            target=prov.start,
            args=(stop,),
            kwargs={"interval": 0, "profile_homes": profile_homes},
            daemon=True,
        )
        t.start()
        # Wait for at least len(profile_homes) tick calls (one full cycle).
        deadline = time.monotonic() + 10
        while len(tick_count) < len(profile_homes) and time.monotonic() < deadline:
            time.sleep(0.005)
        # Give one more cycle to ensure it keeps ticking.
        deadline = time.monotonic() + 3
        while len(tick_count) < len(profile_homes) * 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        stop.set()
        t.join(timeout=5)

    assert not t.is_alive()
    # The ticker called tick() at least once per profile per iteration.
    # With 2 profiles and multiple iterations, we should have seen at least 2 calls.
    assert len(tick_count) >= len(profile_homes), \
        f"Expected >= {len(profile_homes)} tick calls, got {len(tick_count)}"
