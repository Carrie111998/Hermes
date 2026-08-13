"""Durable dispatcher singleton-lock handling (t_77f0d093).

Regression tests for the durable dispatcher lock on top of the t_fb4a7ca4
lease/takeover work:

D1 — eligibility gate: only profiles named in ``kanban.dispatch_profiles``
     (default ``["default"]``) may even attempt the singleton lock. Factory
     worker profiles (dev/lead/qa/reviewer), helpers and freshly created
     profiles must NOT race for it, so a misconfigured worker gateway that
     flips ``dispatch_in_gateway`` on cannot steal the lock from the main
     gateway and starve the board.

D2 — stale/dead holder recovery: a contender that found the lock contended
     at boot does not give up forever. It re-checks on
     ``kanban.lock_takeover_interval``; when the holder's process died the
     kernel already released the flock, so the retry acquires it (board
     recovers within the recheck interval instead of starving until a
     container restart). A stale heartbeat with a live pid is a wedged
     dispatcher loop — a live flock cannot be stolen — so the contender
     warns and keeps retrying instead of pretending to take over.

D3 — non-factory-profile lock stealing: when the lease shows a holder whose
     profile is not allowed to dispatch, the contender writes a takeover
     challenge into the lease file and the holder (running this code)
     releases the lock on its next tick and stands down, so the
     challenger's retry acquires it. A healthy, entitled holder is never
     touched.

D4 — holder-side periodic recheck: the lock owner re-reads its own lease
     every tick, refreshes the heartbeat, and steps down (releasing the
     flock) the moment it is no longer dispatch-eligible or an eligible
     contender challenged it. This is the holder half of "restarting a
     non-factory gateway no longer makes the main gateway lose the lock".
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.kanban_watchers import (
    GatewayKanbanWatchersMixin,
    _acquire_singleton_lock,
    _dispatcher_eligible_profiles,
    _dispatcher_holder_should_step_down,
    _dispatcher_holder_stealable,
    _dispatcher_lease_identity,
    _dispatcher_lease_profile,
    _dispatcher_profile_eligible,
    _dispatcher_takeover_challenge,
    _lease_owner_alive,
    _read_dispatcher_lease,
    _release_singleton_lock,
    _write_dispatcher_lease,
)

DEFAULT_CFG = {
    "dispatch_in_gateway": True,
    "dispatch_profiles": ["default"],
    "lock_takeover_interval": 30,
    "lock_lease_timeout": 120,
}


def _lease_stub(profile="default"):
    """A stand-in for the mixin with just the lease identity attributes."""
    return SimpleNamespace(
        _kanban_dispatcher_lease_profile=profile,
        _kanban_dispatcher_lease_started_at=int(time.time()),
    )


def _spawn_holder(lock_path: Path, profile: str, sleep: float = 60.0):
    """Spawn a subprocess that flocks *lock_path* and writes a lease record.

    Returns the Popen handle. The subprocess owns the real OS flock, so the
    parent's ``_acquire_singleton_lock`` returns ``contended`` until it dies.
    """
    script = (
        "import fcntl, json, os, sys, time\n"
        f"p = {str(lock_path)!r}\n"
        "f = open(p, 'a+')\n"
        "assert fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB) is None\n"
        "rec = {'version': 1, 'profile': %r, 'pid': os.getpid(),\n"
        "       'host': 'test-host', 'started_at': int(time.time()),\n"
        "       'heartbeat_at': int(time.time())}\n"
        "f.seek(0); f.truncate(); f.write(json.dumps(rec)); f.flush()\n"
        "print('LOCKED', flush=True)\n"
        "time.sleep(%r)\n"
    ) % (profile, sleep)
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    line = proc.stdout.readline().strip()
    assert line == "LOCKED", f"holder subprocess failed: {line!r}"
    return proc


# ---------------------------------------------------------------------------
# D1: dispatch-eligibility gate
# ---------------------------------------------------------------------------


def test_eligibility_gate_defaults_to_default_profile():
    """Only the default profile is eligible unless dispatch_profiles opts in."""
    assert _dispatcher_eligible_profiles({}) == ["default"]
    assert _dispatcher_eligible_profiles({"dispatch_profiles": ["default"]}) == ["default"]
    assert _dispatcher_profile_eligible("default", {}) is True
    # Factory worker profiles must NOT race for the lock.
    for worker in ("factory-dev", "factory-lead", "factory-qa", "factory-reviewer", "helper"):
        assert _dispatcher_profile_eligible(worker, {}) is False, worker
    # Explicit opt-in is the only way a second dispatcher is allowed.
    cfg = {"dispatch_profiles": ["default", "factory-dispatcher"]}
    assert _dispatcher_profile_eligible("factory-dispatcher", cfg) is True
    assert _dispatcher_profile_eligible("factory-reviewer", cfg) is False
    # An unknown/label-less profile is never eligible.
    assert _dispatcher_profile_eligible("unknown", {}) is False
    assert _dispatcher_profile_eligible("", {}) is False
    # String form is accepted for convenience.
    assert _dispatcher_eligible_profiles({"dispatch_profiles": "default"}) == ["default"]


def test_lease_profile_labeling_for_gateways(monkeypatch):
    """A gateway launched without -p is the default profile, not 'unknown'."""
    from hermes_cli import profiles
    # Shared-HERMES_HOME deployment: every gateway's home resolves to the
    # default, so the argv -p flag is the per-gateway signal.
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(
        sys, "argv", ["hermes", "gateway", "run"],
    )
    assert _dispatcher_lease_profile() == "default"
    monkeypatch.setattr(
        sys, "argv", ["hermes", "-p", "factory-reviewer", "gateway", "run"],
    )
    assert _dispatcher_lease_profile() == "factory-reviewer"
    monkeypatch.setattr(
        sys, "argv", ["hermes", "gateway", "run", "--profile", "factory-qa"],
    )
    assert _dispatcher_lease_profile() == "factory-qa"


# ---------------------------------------------------------------------------
# D2: stale/dead holder recovery
# ---------------------------------------------------------------------------


def test_holder_stealable_verdicts():
    """The reclaimability verdict: dead / stale / non_factory / healthy."""
    # Healthy, entitled holder: never touch.
    healthy = {
        "pid": os.getpid(),
        "profile": "default",
        "heartbeat_at": int(time.time()),
    }
    assert _dispatcher_holder_stealable(healthy, DEFAULT_CFG) is None
    # Missing lease record: flock-only takeover (retry), nothing to judge.
    assert _dispatcher_holder_stealable({}, DEFAULT_CFG) is None
    # Dead pid -> dead.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=10)
    assert _dispatcher_holder_stealable(
        {"pid": dead.pid, "profile": "default", "heartbeat_at": int(time.time())},
        DEFAULT_CFG,
    ) == "dead"
    # Alive pid + stale heartbeat -> stale (wedged loop; flock not stealable).
    stale = {
        "pid": os.getpid(),
        "profile": "default",
        "heartbeat_at": int(time.time()) - 10_000,
    }
    assert _dispatcher_holder_stealable(stale, DEFAULT_CFG) == "stale"
    # Alive + heartbeating but a non-dispatch profile -> non_factory.
    misconfigured = {
        "pid": os.getpid(),
        "profile": "factory-reviewer",
        "heartbeat_at": int(time.time()),
    }
    assert _dispatcher_holder_stealable(misconfigured, DEFAULT_CFG) == "non_factory"
    # non_factory beats stale: a misconfigured holder must be challenged
    # even when its heartbeat also went stale.
    misconfigured_stale = {
        "pid": os.getpid(),
        "profile": "factory-reviewer",
        "heartbeat_at": int(time.time()) - 10_000,
    }
    assert _dispatcher_holder_stealable(misconfigured_stale, DEFAULT_CFG) == "non_factory"


def test_dead_holder_lock_recovered_on_recheck(tmp_path):
    """A dead holder's flock is released by the kernel; the next recheck
    acquires it — the board recovers within the recheck interval."""
    lock_path = tmp_path / "dispatcher.lock"
    proc = _spawn_holder(lock_path, profile="factory-reviewer")
    try:
        # Alive holder -> contended.
        h, s = _acquire_singleton_lock(lock_path)
        assert s == "contended"
        assert h is None
        lease = _read_dispatcher_lease(lock_path)
        assert _dispatcher_holder_stealable(lease, DEFAULT_CFG) in (
            "non_factory", "dead",
        )
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # Dead holder -> the retry acquire (the takeover loop's next recheck)
    # succeeds immediately.
    h, s = _acquire_singleton_lock(lock_path)
    assert s == "held", "flock must be acquirable right after owner death"
    _release_singleton_lock(h)


def test_stale_holder_not_stolen_while_alive(tmp_path):
    """A live pid with a stale heartbeat is a wedged loop: warn, never steal."""
    lock_path = tmp_path / "dispatcher.lock"
    proc = _spawn_holder(lock_path, profile="default")
    try:
        # Age the heartbeat in the lease file (holder is still alive).
        lease = _read_dispatcher_lease(lock_path)
        lease["heartbeat_at"] = int(time.time()) - 10_000
        lock_path.write_text(json.dumps(lease), encoding="utf-8")
        assert _dispatcher_holder_stealable(lease, DEFAULT_CFG) == "stale"
        # The flock is still held by the live process -> contended.
        h, s = _acquire_singleton_lock(lock_path)
        assert s == "contended"
        assert h is None
        # No challenge is written for a mere stale verdict, and the holder
        # does not step down without one.
        assert _dispatcher_holder_should_step_down(
            _read_dispatcher_lease(lock_path), self_eligible=True,
        ) is False
    finally:
        proc.kill()
        proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# D3: non-factory-profile lock stealing
# ---------------------------------------------------------------------------


def test_non_factory_holder_challenge_and_steal(tmp_path):
    """A misconfigured non-dispatch holder is reclaimed cooperatively:
    contender challenges -> holder steps down -> contender's retry acquires."""
    lock_path = tmp_path / "dispatcher.lock"

    # The misconfigured holder (e.g. the factory-reviewer gateway that
    # inherited dispatch_in_gateway=true): alive, heartbeating, wrong profile.
    holder_handle, holder_state = _acquire_singleton_lock(lock_path)
    assert holder_state == "held"
    try:
        _write_dispatcher_lease(
            holder_handle, _dispatcher_lease_identity(_lease_stub("factory-reviewer")),
        )

        # Contender (the default gateway): the holder is stealable.
        lease = _read_dispatcher_lease(lock_path)
        assert _dispatcher_holder_stealable(lease, DEFAULT_CFG) == "non_factory"

        # The contender writes a takeover challenge; a live flock cannot be
        # stolen, so its acquire stays contended until the holder yields.
        _dispatcher_takeover_challenge(lock_path, "non_factory", "default")
        h, s = _acquire_singleton_lock(lock_path)
        assert s == "contended"
        assert h is None

        # Holder side (running the durable code): the challenge plus its own
        # non-eligibility both trigger step-down on the next tick.
        challenged = _read_dispatcher_lease(lock_path)
        assert challenged.get("challenge", {}).get("by") == "default"
        assert _dispatcher_holder_should_step_down(
            challenged, self_eligible=True, challenger_eligible=True,
        ) is True
        assert _dispatcher_holder_should_step_down(
            challenged, self_eligible=False,
        ) is True
        # A challenge from a profile that is itself not allowed to dispatch
        # must NOT bounce a healthy holder.
        assert _dispatcher_holder_should_step_down(
            challenged, self_eligible=True, challenger_eligible=False,
        ) is False

        # The holder releases (its _release_kanban_dispatcher_lock path
        # truncates the lease, mirroring the mixin).
        holder_handle.seek(0)
        holder_handle.truncate()
        holder_handle.flush()
        _release_singleton_lock(holder_handle)
        holder_handle = None

        # The contender's next periodic recheck acquires the freed flock.
        h, s = _acquire_singleton_lock(lock_path)
        assert s == "held"
        _write_dispatcher_lease(h, _dispatcher_lease_identity(_lease_stub("default")))
        lease = _read_dispatcher_lease(lock_path)
        assert lease.get("profile") == "default"
        _release_singleton_lock(h)
    finally:
        if holder_handle is not None:
            _release_singleton_lock(holder_handle)


def test_healthy_holder_never_challenged(tmp_path):
    """A healthy, entitled holder is left alone: no challenge, no step-down."""
    lock_path = tmp_path / "dispatcher.lock"
    holder_handle, s = _acquire_singleton_lock(lock_path)
    assert s == "held"
    try:
        _write_dispatcher_lease(
            holder_handle, _dispatcher_lease_identity(_lease_stub("default")),
        )
        lease = _read_dispatcher_lease(lock_path)
        assert _dispatcher_holder_stealable(lease, DEFAULT_CFG) is None
        assert _dispatcher_holder_should_step_down(lease, self_eligible=True) is False
        assert "challenge" not in _read_dispatcher_lease(lock_path)
    finally:
        _release_singleton_lock(holder_handle)


# ---------------------------------------------------------------------------
# D4: the dispatcher watcher loop (boot-contended -> takeover -> step-down)
# ---------------------------------------------------------------------------


class _FakeRunner(GatewayKanbanWatchersMixin):
    """Minimal stand-in for GatewayRunner: just the attrs the dispatcher
    watcher touches (plus the mixin's lock helpers), with the heavy tick
    internals stubbed via monkeypatch in the test."""

    def __init__(self):
        self._running = True
        self._kanban_dispatcher_lock_handle = None


class _InstantAsync:
    """asyncio stand-in whose sleep() yields once and returns instantly, so
    the watcher's retry loop and tick cadence run without wall-clock waits
    while still letting the test coroutine interleave on the event loop."""

    @staticmethod
    async def sleep(_seconds):
        import asyncio
        await asyncio.sleep(0)

    def __getattr__(self, name):
        import asyncio
        return getattr(asyncio, name)


def _patch_watcher_deps(monkeypatch, tmp_path, kanban_cfg):
    """Route the watcher's kanban_db / config / sleep deps away from the
    live board and the live dispatcher lock, and make time instant."""
    import gateway.kanban_watchers as kw
    from hermes_cli import kanban_db as kb
    import hermes_cli.config as hc_config

    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kb, "list_boards", lambda *a, **kw: [])
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "review_dispatch_enabled", lambda: False)
    monkeypatch.setattr(hc_config, "load_config", lambda: {"kanban": kanban_cfg})
    monkeypatch.setattr(kw, "_kanban_dispatch_allowed", lambda: True)
    # The test process may itself run under a non-default HERMES_HOME (e.g.
    # a factory worker profile); pin the gateway identity the watcher sees.
    monkeypatch.setattr(kw, "_dispatcher_lease_profile", lambda: "default")
    monkeypatch.setattr(kw, "asyncio", _InstantAsync())
    return kw


@pytest.mark.asyncio
async def test_watcher_contended_at_boot_takes_over_after_holder_death(
    tmp_path, monkeypatch,
):
    """Requirement (a) end-to-end: a gateway that found the lock contended
    at boot does NOT give up — when the holder dies, the next recheck
    acquires the lock and the dispatcher comes up."""
    lock_path = tmp_path / "kanban" / ".dispatcher.lock"
    lock_path.parent.mkdir(parents=True)
    holder = _spawn_holder(lock_path, profile="factory-reviewer")

    cfg = {
        "dispatch_in_gateway": True,
        "dispatch_profiles": ["default"],
        "lock_takeover_interval": 30,
        "lock_lease_timeout": 120,
        "dispatch_interval_seconds": 1,
        "auto_decompose": False,
    }
    kw = _patch_watcher_deps(monkeypatch, tmp_path, cfg)
    runner = _FakeRunner()

    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        # Boot: contended (holder alive) -> the watcher must NOT return.
        for _ in range(50):
            await asyncio.sleep(0)
        assert runner._kanban_dispatcher_lock_handle is None
        assert not task.done(), "watcher must keep retrying, not give up at boot"

        # Holder dies -> the retry acquires the freed flock.
        holder.kill()
        holder.wait(timeout=10)
        for _ in range(200):
            if runner._kanban_dispatcher_lock_handle is not None:
                break
            await asyncio.sleep(0)
        assert runner._kanban_dispatcher_lock_handle is not None, (
            "watcher must take over the lock within the recheck interval"
        )
        lease = _read_dispatcher_lease(lock_path)
        assert lease.get("profile") == "default"
        assert lease.get("pid") == os.getpid()
        assert isinstance(lease.get("heartbeat_at"), int)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_watcher_holder_steps_down_on_takeover_challenge(tmp_path, monkeypatch):
    """Requirement (b)/(c) end-to-end, holder half: after acquiring the
    lock, the watcher re-reads the lease every tick and releases + stands
    down when an eligible contender challenges it (non_factory steal)."""
    lock_path = tmp_path / "kanban" / ".dispatcher.lock"
    lock_path.parent.mkdir(parents=True)

    cfg = {
        "dispatch_in_gateway": True,
        "dispatch_profiles": ["default"],
        "lock_takeover_interval": 30,
        "lock_lease_timeout": 120,
        "dispatch_interval_seconds": 1,
        "auto_decompose": False,
    }
    kw = _patch_watcher_deps(monkeypatch, tmp_path, cfg)
    runner = _FakeRunner()

    task = asyncio.create_task(runner._kanban_dispatcher_watcher())
    try:
        # Acquire (no contention at boot).
        for _ in range(200):
            if runner._kanban_dispatcher_lock_handle is not None:
                break
            await asyncio.sleep(0)
        assert runner._kanban_dispatcher_lock_handle is not None
        lease = _read_dispatcher_lease(lock_path)
        assert lease.get("profile") == "default"

        # A contender challenges the lease (simulating a more privileged
        # gateway taking over a misconfigured holder).
        _dispatcher_takeover_challenge(lock_path, "non_factory", "default")

        # The holder's next tick sees the challenge and stands down.
        for _ in range(200):
            if runner._kanban_dispatcher_lock_handle is None:
                break
            await asyncio.sleep(0)
        assert runner._kanban_dispatcher_lock_handle is None, (
            "holder must release the lock when challenged"
        )
        assert task.done(), "watcher must return after standing down"
        # The lock is free for the challenger's retry.
        h, s = _acquire_singleton_lock(lock_path)
        assert s == "held"
        _release_singleton_lock(h)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
