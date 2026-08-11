"""Serve parent-death watchdog must not self-reap a healthy backend — #83555.

`hermes serve` spawned by Hermes Desktop exits 0 before printing
HERMES_BACKEND_READY on Windows, so the desktop never boots and its repair
path escalates to a venv reinstall that cannot fix it. Cause: the watchdog
treated *any* ppid change as orphaning, but on a uv `relocatable = true` venv
`venv\\Scripts\\python.exe` is a trampoline that launches the real interpreter
as its own child — so getppid() is always the trampoline, never the desktop
process recorded in HERMES_PARENT_PID, and the watchdog fired on the first
poll of a completely healthy install.

These tests pin the distinction the fix rests on: a changed ppid is evidence
of orphaning, a dead recorded parent is proof.
"""

import hermes_cli.web_server as ws


ALIVE = lambda _pid: True  # noqa: E731 - probe stubs, terse on purpose
DEAD = lambda _pid: False  # noqa: E731


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_trampoline_interposed_parent_is_not_orphaned():
    """The #83555 case: ppid is the trampoline, desktop parent still alive."""
    assert ws._is_serve_orphaned(4242, getppid=lambda: 777, pid_exists=ALIVE) is False


def test_unchanged_ppid_is_not_orphaned_without_probing():
    """The common POSIX case short-circuits — no probe call at all."""
    calls = []

    def _probe(pid):
        calls.append(pid)
        return True

    assert ws._is_serve_orphaned(42, getppid=lambda: 42, pid_exists=_probe) is False
    assert calls == []


# ---------------------------------------------------------------------------
# The behavior that must NOT regress: real orphaning still self-reaps
# ---------------------------------------------------------------------------


def test_dead_recorded_parent_is_orphaned():
    """a9a0648f4's purpose: parent really died, so reap this backend."""
    assert ws._is_serve_orphaned(4242, getppid=lambda: 1, pid_exists=DEAD) is True


def test_reparented_to_init_with_dead_parent_is_orphaned():
    """POSIX: ppid becomes 1 and the recorded parent is gone."""
    assert ws._is_serve_orphaned(31337, getppid=lambda: 1, pid_exists=DEAD) is True


# ---------------------------------------------------------------------------
# Fail-safe: a broken probe must not kill a healthy backend
# ---------------------------------------------------------------------------


def test_probe_failure_reports_not_orphaned():
    """A stray backend is recoverable; a desktop that never boots is not."""

    def _boom(_pid):
        raise OSError("access denied")

    assert ws._is_serve_orphaned(4242, getppid=lambda: 777, pid_exists=_boom) is False


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_default_probe_is_the_non_killing_helper(monkeypatch):
    """Must not fall back to os.kill(pid, 0): on Windows CPython routes signal
    0 through GenerateConsoleCtrlEvent, so the 'harmless' liveness probe can
    actually signal the target."""
    seen = []

    import gateway.status as status

    monkeypatch.setattr(status, "_pid_exists", lambda pid: seen.append(pid) or True)
    assert ws._is_serve_orphaned(4242, getppid=lambda: 777) is False
    assert seen == [4242]


def test_watchdog_is_noop_without_parent_pid(monkeypatch):
    """Standalone `hermes serve` must never start the watchdog thread."""
    monkeypatch.delenv("HERMES_PARENT_PID", raising=False)
    started = []
    monkeypatch.setattr(
        ws.threading, "Thread", lambda *a, **k: started.append(k) or _NoThread()
    )
    ws._start_parent_death_watchdog()
    assert started == []


class _NoThread:
    def start(self):  # pragma: no cover - only reached on regression
        raise AssertionError("watchdog thread must not start")


def test_watchdog_starts_when_desktop_records_a_parent(monkeypatch):
    monkeypatch.setenv("HERMES_PARENT_PID", "4242")
    started = {}

    class _Thread:
        def __init__(self, **kwargs):
            started.update(kwargs)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(ws.threading, "Thread", lambda *a, **k: _Thread(**k))
    ws._start_parent_death_watchdog()
    assert started.get("started") is True
    assert started.get("daemon") is True
    assert started.get("name") == "serve-parent-watchdog"


def test_watchdog_ignores_a_malformed_parent_pid(monkeypatch):
    monkeypatch.setenv("HERMES_PARENT_PID", "not-a-pid")
    monkeypatch.setattr(
        ws.threading, "Thread", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )
    ws._start_parent_death_watchdog()  # must not raise
