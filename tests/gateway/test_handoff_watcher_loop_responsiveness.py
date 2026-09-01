"""Regression guard: ``_handoff_watcher`` must not block the event loop on
synchronous profile secret-scope filesystem I/O.

Issue #100014 (``_handoff_watcher`` can block the multiplex gateway loop in
profile secret loading and trigger watchdog exit 75) reports a single
synchronous ``Path.read_text`` reaching the asyncio thread from inside the
watcher's per-tick scope entry. Under a slow filesystem (Termux+PRoot, WSL2
VHDX, NFS) the read can take 10s+; the loop-liveness watchdog counts three
misses and the gateway hard-exits with ``os._exit(75)`` — taking every
multiplexed profile with it.

The fix is approach (1) from the issue's suggested-fix list: the watcher
moves the blocking secret-scope construction off the event loop with
``asyncio.to_thread`` at the async call boundary, and approach (2): it
caches the immutable profile secret mapping so a tick that does NOT need
to re-read ``<home>/.env`` never touches the filesystem at all.

These tests pin BOTH halves of the fix:

  1. ``test_watcher_loop_stays_responsive_under_slow_env_read`` —
     a deliberately slow ``Path.read_text`` on the profile's ``.env``
     must NOT prevent an independent ticker from progressing while
     the watcher is mid-tick. Without the fix the loop freezes for
     the duration of the read and the ticker never fires.

  2. ``test_watcher_caches_profile_secret_scope_across_ticks`` —
     on the second tick the watcher must NOT re-read ``<home>/.env``
     when the file mtime and resolved secret mapping are unchanged.
     Without the cache the watcher's 2-second tick re-reads the file
     every time, multiplying the stall exposure across all multiplexed
     profiles.

Both tests drive the real ``GatewayRunner._handoff_watcher`` through the
real ``_profile_runtime_scope`` context manager and the real
``build_profile_secret_scope`` — only ``load_env_file`` and one call to
``Path.read_text`` are replaced with a fixture that records the read and
optionally stalls. Anything less would be a mock of the fix, not of the
bug.

Why a separate file (and not added to ``test_handoff_watcher_resilience``
or ``test_handoff_watcher_multiprofile``): those files pin the async
dispatch semantics and the scope iteration; this file pins the
loop-responsiveness contract. Mixing the two would let a future fix
that solves one regress the other while keeping the test green.
"""

import asyncio
import os
import time
import types
from pathlib import Path

import pytest

from gateway import run


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _TickingProbe:
    """Records how many loop ticks complete while the watcher is mid-poll.

    The probe is started just before the watcher and stopped when the test
    ends. If the event loop is frozen (by a synchronous ``Path.read_text``
    on the gateway loop thread), the probe's tick counter stalls — that
    is the regression we are guarding against.

    Each tick records its wall-clock timestamp so the test can ask for
    the number of ticks that fired INSIDE a specific window — the stall
    window the watcher would block through if the bug were present.
    """

    def __init__(self, interval_s: float = 0.01):
        self.interval_s = interval_s
        self.tick_times: list = []
        self._task = None
        self._stop = asyncio.Event()

    async def _run(self):
        try:
            while not self._stop.is_set():
                self.tick_times.append(time.monotonic())
                await asyncio.sleep(self.interval_s)
        except asyncio.CancelledError:
            pass

    async def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await asyncio.wait_for(self._task, timeout=2)

    def ticks_in(self, t0: float, t1: float) -> int:
        """How many ticks landed inside ``[t0, t1]`` (monotonic seconds)."""
        return sum(1 for t in self.tick_times if t0 <= t <= t1)


class _FakeDB:
    """Yields nothing — the watcher just needs to enter the per-profile scope."""

    def __init__(self):
        self.polls = 0

    async def list_pending_handoffs(self):
        self.polls += 1
        return []

    async def claim_handoff(self, _sid):
        return False


class _RecordingReader:
    """Replace ``Path.read_text`` for the profile's ``.env`` only.

    The replacement records every call and, when ``stall_s`` is non-zero,
    blocks the calling thread for that long. Each blocked call records
    its monotonic ``stall_start`` / ``stall_end`` so the test can assert
    that an independent ticker fired during that exact window — that
    is the regression gate the watchdog cares about.
    """

    def __init__(self, real_read_text, stall_s: float = 0.0):
        self._real_read_text = real_read_text
        self.stall_s = stall_s
        self.calls = 0
        self.stall_intervals: list = []

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.stall_s > 0:
            t0 = time.monotonic()
            time.sleep(self.stall_s)
            t1 = time.monotonic()
            self.stall_intervals.append((t0, t1))
        return self._real_read_text(*args, **kwargs)


async def _noop_handoff(_row, _profile_name=None):
    return None


# ---------------------------------------------------------------------------
# ContextVar propagation through the async scope seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_scope_propagates_context_to_dispatch_task(tmp_path):
    """Off-loop secret loading must NOT move the installed ContextVars
    off-task (the #91217 invariant, re-pinned for the async seam).

    ``_handoff_watcher`` creates its dispatch tasks INSIDE the profile
    scope but they RUN after it exits. They still see the profile's home
    and secret scope only because ``set_hermes_home_override`` and
    ``set_secret_scope`` are ContextVar-based and ``ensure_future`` copies
    the current Context into the Task. If the async variant had installed
    the overrides on the worker THREAD's context (or leaked them as module
    globals), secondary-profile handoffs would silently deliver through
    the primary profile's credentials.
    """
    import threading

    from agent import secret_scope as secret_scope_mod
    from hermes_constants import get_hermes_home

    profile_home = tmp_path / "profiles" / "scoped"
    profile_home.mkdir(parents=True)
    secrets = {"PROFILE_TOKEN": "scoped-value"}

    loop_thread = threading.get_ident()
    load_threads: list = []

    def _fake_cached_resolve(home):
        load_threads.append(threading.get_ident())
        return secrets

    original_scope = secret_scope_mod.current_secret_scope()
    original_home = get_hermes_home()
    inherited: list = []

    async def _capture_context():
        inherited.append(
            (secret_scope_mod.current_secret_scope(), get_hermes_home())
        )

    from unittest.mock import patch

    with patch.object(
        run, "_resolve_profile_secret_scope_cached", _fake_cached_resolve
    ):
        async with run._profile_runtime_scope_async(profile_home):
            # Inside the scope: BOTH ContextVars point at the profile.
            assert secret_scope_mod.current_secret_scope() is secrets
            assert get_hermes_home() == profile_home
            dispatch = asyncio.create_task(_capture_context())

    await dispatch

    # The load ran OFF the loop thread (the whole point of the fix)...
    assert load_threads and all(t != loop_thread for t in load_threads), (
        "secret-scope construction must run on a worker thread via "
        "asyncio.to_thread, not on the event loop (issue #100014)"
    )
    # ...but the installed scope propagated INTO the dispatch task via the
    # copied Context — the #91217 invariant.
    assert inherited == [(secrets, profile_home)], (
        "dispatch task created inside the async scope must inherit the "
        "profile's secret scope and home override"
    )
    # And the scope was fully restored on exit.
    assert secret_scope_mod.current_secret_scope() is original_scope
    assert get_hermes_home() == original_home


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watcher_loop_stays_responsive_under_slow_env_read(
    tmp_path, monkeypatch
):
    """A slow ``.env`` read inside the per-profile scope MUST NOT freeze
    the gateway event loop.

    This is the literal failure mode from issue #100014 — the watcher's
    per-tick ``_profile_runtime_scope`` enters ``build_profile_secret_scope``
    which calls ``Path.read_text`` synchronously. We slow the read down
    by ``stall_s`` seconds and assert that an independent ticker can
    still progress while the watcher is mid-poll. Without the fix the
    tick counter is 0 for the whole stall; with the fix it grows
    normally.
    """
    # Lay down a real profile directory tree the watcher can resolve.
    profiles_home = tmp_path / "profiles"
    bala_home = profiles_home / "bala"
    bala_home.mkdir(parents=True)
    (bala_home / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=stale\n", encoding="utf-8"
    )

    # Force multiplex scope resolution to a single secondary profile so
    # we exercise the per-profile scope entry path.
    scopes = [
        (None, None),
        ("bala", bala_home),
    ]
    monkeypatch.setattr(run, "_handoff_watch_scopes", lambda _r: scopes)

    # Install a recording reader that stalls ``stall_s`` seconds. We
    # wake it as soon as the probe has observed a tick, so the test
    # completes in roughly ``probe_ticks * probe_interval`` wall time.
    stall_s = 0.5
    import agent.secret_scope as secret_scope_mod

    real_read_text = secret_scope_mod.Path.read_text
    recorder = _RecordingReader(real_read_text, stall_s=stall_s)

    # Patch Path.read_text globally; the per-profile ``<home>/.env`` is
    # the only file the watcher reads through this codepath.
    def _patched_read_text(self, *args, **kwargs):
        if str(self) == str(bala_home / ".env"):
            return recorder(*args, **kwargs)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(secret_scope_mod.Path, "read_text", _patched_read_text)

    # And patch the import inside ``agent.secret_scope`` (the load_env_file
    # uses a module-local Path reference on some Python builds; the
    # production path is Path(env_path).read_text(...) which resolves via
    # the bound class).
    monkeypatch.setattr(
        "pathlib.Path.read_text",
        _patched_read_text,
        raising=False,
    )

    # Run the watcher for a bounded window: long enough to enter the
    # per-profile scope at least once, short enough to keep the test fast.
    # The watcher has a 5s initial delay (gateway-connect wait) and a
    # configurable per-tick interval. We use ``interval=0.5`` so the
    # watcher exits after one tick + one sleep once ``_running`` flips
    # False, keeping the test under ~7s total. The probe task runs
    # concurrently and its tick counter is the regression gate.
    db = _FakeDB()
    states = iter([True] + [False])

    class _Running:
        def __bool__(_self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner = types.SimpleNamespace()
    runner._session_db = db
    runner._running = _Running()
    runner._process_handoff = _noop_handoff

    probe = _TickingProbe(interval_s=0.01)

    # Start the probe BEFORE the watcher so the probe's tick task is
    # already scheduled when the watcher's tick runs. The watcher is
    # run as a separate task so the probe can advance during the
    # watcher's ``asyncio.sleep(interval)`` between ticks AND during
    # the slow read (only with the fix).
    await probe.start()
    watcher_task = asyncio.create_task(
        run.GatewayRunner._handoff_watcher(runner, interval=0.5)
    )
    try:
        await asyncio.wait_for(watcher_task, timeout=15.0)
    finally:
        await probe.stop()

    # Core invariant: the loop made forward progress DURING the stalled
    # ``.env`` read — not just overall. Without the fix, the watcher's
    # per-profile scope entry blocks the entire event loop, so the probe
    # records ZERO ticks inside the ``stall_intervals`` window. With the
    # fix, the read runs on a thread and the probe runs freely on the
    # loop, producing ~``stall_s / probe.interval`` ticks.
    assert recorder.calls >= 1, (
        "watcher must have entered the per-profile scope at least once "
        "and triggered the slowed .env read"
    )
    assert recorder.stall_intervals, (
        "the slowed .env read must have actually fired at least once"
    )
    ticks_during_stall = sum(
        probe.ticks_in(t0, t1) for t0, t1 in recorder.stall_intervals
    )
    assert ticks_during_stall >= 3, (
        f"event loop appears frozen during a slow profile .env read — "
        f"probe recorded only {ticks_during_stall} ticks during the "
        f"stalled read(s) {recorder.stall_intervals} "
        f"(issue #100014 regression: synchronous Path.read_text on "
        f"the gateway loop freezes the event loop)"
    )


@pytest.mark.asyncio
async def test_watcher_caches_profile_secret_scope_across_ticks(
    tmp_path, monkeypatch
):
    """The watcher must not re-read ``<home>/.env`` on every tick when the
    file is unchanged.

    Issue #100014's approach (2): cache the resolved secret mapping per
    profile so a 2-second tick does not multiply the stall exposure
    across the multiplex profile set. This test asserts that, given an
    unchanged file, the watcher reads it AT MOST ONCE per process —
    even across multiple ticks.
    """
    profiles_home = tmp_path / "profiles"
    bala_home = profiles_home / "bala"
    bala_home.mkdir(parents=True)
    env_path = bala_home / ".env"
    env_path.write_text("TELEGRAM_BOT_TOKEN=token\n", encoding="utf-8")

    scopes = [
        (None, None),
        ("bala", bala_home),
    ]
    monkeypatch.setattr(run, "_handoff_watch_scopes", lambda _r: scopes)

    import agent.secret_scope as secret_scope_mod

    real_read_text = secret_scope_mod.Path.read_text
    env_reads = {"count": 0}

    def _patched_read_text(self, *args, **kwargs):
        if str(self) == str(env_path):
            env_reads["count"] += 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(
        secret_scope_mod.Path, "read_text", _patched_read_text
    )
    monkeypatch.setattr(
        "pathlib.Path.read_text",
        _patched_read_text,
        raising=False,
    )

    db = _FakeDB()
    # Drive the watcher for several ticks (the existing test in
    # test_handoff_watcher_multiprofile uses ``states = iter([True, False])``
    # for two ticks; we extend to several so a missing cache produces a
    # 1-read-per-tick explosion).
    states = iter([True] * 6 + [False])

    class _Running:
        def __bool__(_self):
            try:
                return next(states)
            except StopIteration:
                return False

    runner = types.SimpleNamespace()
    runner._session_db = db
    runner._running = _Running()
    runner._process_handoff = _noop_handoff

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(run.asyncio, "sleep", _no_sleep)

    await asyncio.wait_for(
        run.GatewayRunner._handoff_watcher(runner, interval=0.0),
        timeout=5.0,
    )

    # The watcher must read the .env at most once across multiple ticks
    # — the file is immutable for the lifetime of a gateway process
    # unless its mtime changes. (Startup reclaim + first tick would be
    # a defensible ``<=2``; a healthy cache is exactly 1.)
    assert env_reads["count"] <= 1, (
        f"watcher re-read the profile .env {env_reads['count']} times "
        f"across multiple ticks — must cache the secret scope per "
        f"process and only refresh on mtime change (issue #100014 "
        f"approach 2)"
    )
