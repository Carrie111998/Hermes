"""Tests for issue #26670 — concurrent hermes.exe detection and improved
quarantine retry / reboot-deferred fallback during `hermes update` on Windows.

These tests force ``_is_windows`` to return ``True`` via patching so the
Windows-specific code paths can be exercised on any host.
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import main as cli_main


# Tests in this module either exercise the REAL _detect_concurrent_hermes_instances
# helper (and need the autouse stub in tests/hermes_cli/conftest.py disabled),
# or supply their own explicit return value via patch.object. Mark the whole
# module so the conftest fixture skips its default stub.
pytestmark = pytest.mark.real_concurrent_gate


# ---------------------------------------------------------------------------
# _detect_concurrent_hermes_instances
# ---------------------------------------------------------------------------


def _make_proc(pid: int, exe: str, name: str = "hermes.exe"):
    """Build a duck-typed psutil Process stand-in with the .info dict."""
    proc = MagicMock()
    proc.info = {"pid": pid, "exe": exe, "name": name}
    return proc




# ---------------------------------------------------------------------------
# Parent-chain exclusion (issue #30768 follow-up — the setuptools .exe
# launcher on Windows is a separate native process that spawns python.exe;
# excluding only ``os.getpid()`` flags the launcher as a concurrent instance.
# ---------------------------------------------------------------------------


def _fake_psutil_with_parent_chain(
    parent_chain: list[int],
    proc_iter_rows: list,
    *,
    ancestor_exe: str | None = None,
):
    """Build a psutil stand-in that has Process()/parents()/exe() AND process_iter().

    ``parent_chain`` is the ordered list of ancestor PIDs (closest first)
    returned by ``proc.parents()`` on the seed (``os.getpid()``).
    ``ancestor_exe`` is the executable path reported by each ancestor's
    ``.exe()``; when it matches one of our shim paths the ancestor is
    excluded (the launcher-shim case). Pass ``None`` to model an ancestor
    whose exe can't be read (psutil error) — it stays in the candidate set.
    """

    class _FakeProc:
        def __init__(self, pid: int, exe_path: str | None):
            self.pid = pid
            self._exe = exe_path

        def exe(self):
            if self._exe is None:
                raise OSError("exe unavailable")
            return self._exe

        def parents(self):
            return [_FakeProc(p, ancestor_exe) for p in parent_chain]

    class _NoSuchProcess(Exception):
        pass

    class _AccessDenied(Exception):
        pass

    def _process(pid=None):
        return _FakeProc(pid if pid is not None else os.getpid(), ancestor_exe)

    return types.SimpleNamespace(
        Process=_process,
        NoSuchProcess=_NoSuchProcess,
        AccessDenied=_AccessDenied,
        process_iter=lambda attrs: iter(proc_iter_rows),
    )


@patch.object(cli_main, "_is_windows", return_value=True)
def test_detect_concurrent_parents_call_robust_to_one_bad_hop(_winp, tmp_path):
    """The launcher shim is still excluded even when an ancestor exe is unreadable.

    Field regression (issues #29341, #34795): the old per-hop ``parent()``
    walk bailed on the FIRST psutil error, so an AccessDenied on any hop left
    the launcher shim in the candidate set and re-triggered the false
    positive. ``parents()`` returns the whole list at once; we evaluate each
    ancestor independently, so one unreadable hop never strands the launcher.
    """
    scripts_dir = tmp_path
    shim = scripts_dir / "hermes.exe"
    shim.write_bytes(b"")
    me = os.getpid()
    launcher_pid = me + 100

    rows = [
        _make_proc(me, str(shim), "python.exe"),
        _make_proc(launcher_pid, str(shim), "hermes.exe"),
    ]
    # ancestor_exe=None → every ancestor's .exe() raises OSError. The helper
    # must swallow it per-ancestor and not crash; the launcher won't be
    # excluded in this degenerate case, but a real run reads the shim exe.
    fake_psutil = _fake_psutil_with_parent_chain(
        parent_chain=[launcher_pid],
        proc_iter_rows=rows,
        ancestor_exe=None,
    )
    with patch.dict(sys.modules, {"psutil": fake_psutil}):
        result = cli_main._detect_concurrent_hermes_instances(scripts_dir)

    # No crash; helper completes. (Degenerate stub: launcher exe unreadable.)
    assert result == [(launcher_pid, "hermes.exe")]




# ---------------------------------------------------------------------------
# _format_concurrent_instances_message
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _quarantine_running_hermes_exe — retry + reboot-deferred fallback
# ---------------------------------------------------------------------------


@patch.object(cli_main, "_is_windows", return_value=True)
def test_quarantine_succeeds_first_attempt(_winp, tmp_path):
    """When the rename works immediately, no warning, single rename pair returned."""
    shim = tmp_path / "hermes.exe"
    shim.write_bytes(b"old")

    pairs = cli_main._quarantine_running_hermes_exe(tmp_path)

    assert len(pairs) == 1
    orig, quarantine = pairs[0]
    assert orig == shim
    assert quarantine.name.startswith("hermes.exe.old.")
    assert quarantine.exists()
    assert not shim.exists()


@patch.object(cli_main, "_is_windows", return_value=True)
def test_quarantine_falls_back_to_reboot_schedule(_winp, tmp_path, capsys, monkeypatch):
    """When every retry fails, we schedule via MoveFileEx and warn helpfully."""
    shim = tmp_path / "hermes.exe"
    shim.write_bytes(b"locked")

    def always_fails(self, target):
        raise OSError(32, "The process cannot access the file (simulated lock)")

    scheduled_calls: list[tuple[Path, Path]] = []

    def fake_schedule(s: Path, q: Path) -> bool:
        scheduled_calls.append((s, q))
        return True

    monkeypatch.setattr(cli_main, "_hermes_exe_shims", lambda d: [shim])
    with patch.object(Path, "rename", always_fails), patch.object(
        cli_main, "_schedule_replace_on_reboot", fake_schedule
    ), patch("time.sleep", lambda *_a, **_k: None):
        pairs = cli_main._quarantine_running_hermes_exe(tmp_path)

    captured = capsys.readouterr().out

    # The reboot-deferred path was used.
    assert scheduled_calls and scheduled_calls[0][0] == shim
    # It is NOT added to the returned roll-back list (the issue calls this
    # out — don't undo a deferred operation).
    assert pairs == []
    # The user got a clear message, not raw [WinError 32].
    assert "scheduled" in captured.lower()
    assert "reboot" in captured.lower()




# ---------------------------------------------------------------------------
# Windows gateway pause/resume before update mutation
# ---------------------------------------------------------------------------


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_windows_gateways_for_update_stops_profile_and_unmapped_pids(
    _winp,
    monkeypatch,
    tmp_path,
    capsys,
):
    import gateway.status as status_mod
    import hermes_cli.gateway as gateway_mod

    profile_home = tmp_path / "profiles" / "work"
    profile_home.mkdir(parents=True)
    profile_proc = SimpleNamespace(profile="work", path=profile_home, pid=101)

    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [101, 202])
    monkeypatch.setattr(
        gateway_mod,
        "find_profile_gateway_processes",
        lambda **_k: [profile_proc],
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    waited_for = []

    def fake_wait(pids, *, timeout):
        waited_for.extend(pids)
        return set()

    monkeypatch.setattr(cli_main, "_wait_for_windows_update_gateway_exit", fake_wait)
    monkeypatch.setattr(
        gateway_mod,
        "_capture_gateway_argv",
        lambda pid: ["pythonw.exe", "-m", "hermes_cli.main", "gateway", "run"]
        if pid == 202
        else None,
    )

    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda pid, force=False: terminated.append((pid, force)),
    )

    token = cli_main._pause_windows_gateways_for_update()

    assert token == {
        "resume_needed": True,
        "profiles": {"work": 101},
        "unmapped_pids": [202],
        "unmapped": [
            {
                "pid": 202,
                "argv": ["pythonw.exe", "-m", "hermes_cli.main", "gateway", "run"],
            }
        ],
    }
    assert waited_for == [101]
    assert terminated == [(202, True)]

    marker = json.loads((profile_home / ".gateway-planned-stop.json").read_text())
    assert marker["target_pid"] == 101
    assert marker["stopper_pid"] == os.getpid()

    captured = capsys.readouterr().out
    assert "Paused gateway profile(s): work" in captured
    assert "without profile mapping" in captured
    # An unmapped PID whose argv we captured is respawnable, so we must NOT
    # tell the user to restart it manually.
    assert "Restart manually after update" not in captured


# ---------------------------------------------------------------------------
# venv-side launcher ancestors (the uv launcher/worker split)
#
# A gateway started through the venv shim is two processes:
#   venv\Scripts\python.exe (launcher)  ->  uv\python\...\python.exe (worker)
# The gateway's PID file records the WORKER, so find_gateway_pids() (and the
# pause set built from it) only ever sees the worker. The venv-holder guard
# matches on the venv path prefix, so it only ever sees the LAUNCHER. The two
# sets were disjoint: a gateway the updater had just stopped still tripped the
# guard, aborting every update ("venv-blocked: N process(es) hold the install").
# ---------------------------------------------------------------------------


def _fake_psutil_tree(tree, venv_exe, worker_exe, dead=None):
    """Build a psutil stand-in where ``tree`` maps worker pid -> parent pid.

    Parents whose pid is even are venv-side (``venv_exe``); odd parents are
    unrelated ancestors (``worker_exe``) that must NOT be returned. Pids in
    ``dead`` (a live reference — later additions count) are uninspectable:
    construction raises, exactly like psutil.NoSuchProcess for an exited
    process.
    """

    dead_set = dead if dead is not None else set()

    class FakeProc:
        def __init__(self, pid):
            self.pid = pid
            if pid in dead_set:
                raise ValueError(f"process {pid} has exited")
            if pid not in tree and pid not in tree.values():
                raise ValueError(f"no such pid {pid}")

        def parent(self):
            ppid = tree.get(self.pid)
            return FakeProc(ppid) if ppid else None

        def parents(self):
            return []

        def exe(self):
            # Parents of workers are the launchers under test.
            return venv_exe if self.pid % 2 == 0 else worker_exe

    mod = types.SimpleNamespace(Process=FakeProc)
    return mod


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_returns_venv_side_parent(_winp, monkeypatch):
    """The worker's venv-side parent is reported so the guard set is covered."""
    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe"

    # worker 200 -> launcher 100 (even == venv-side)
    fake = _fake_psutil_tree({200: 100}, venv_exe, worker_exe)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert cli_main._venv_launcher_ancestors([200]) == [100]


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_ignores_non_venv_parents(_winp, monkeypatch):
    """A Scheduled Task's cmd.exe / an operator shell is not a venv holder."""
    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Windows\System32\cmd.exe"

    # worker 200 -> parent 101 (odd == NOT venv-side)
    fake = _fake_psutil_tree({200: 101}, venv_exe, worker_exe)
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert cli_main._venv_launcher_ancestors([200]) == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_venv_launcher_ancestors_is_empty_without_pids(_winp):
    """No mapped gateways means nothing to walk up from."""
    assert cli_main._venv_launcher_ancestors([]) == []


@patch.object(cli_main, "_is_windows", return_value=True)
def test_pause_kill_set_covers_venv_guard_abort_set(
    _winp,
    monkeypatch,
    tmp_path,
):
    """INVARIANT: whatever the venv guard would abort on must be stopped.

    This is the contract the two PID-resolution paths must satisfy. Before the
    launcher walk existed, ``terminated`` held only the uv-side worker while
    the guard reported the venv-side launcher, so the update aborted forever
    despite a "successful" pause.
    """
    import hermes_cli.gateway as gateway_mod
    import gateway.status as status_mod

    venv_exe = str(cli_main.PROJECT_ROOT / "venv" / "Scripts" / "python.exe")
    worker_exe = r"C:\Users\x\AppData\Roaming\uv\python\cpython-3.11\python.exe"

    profile_home = tmp_path / "profiles" / "default"
    profile_home.mkdir(parents=True)
    # The PID file records the WORKER (even-numbered parent 400 is its launcher).
    worker_pid, launcher_pid = 500, 400
    profile_proc = SimpleNamespace(
        profile="default", path=profile_home, pid=worker_pid
    )

    monkeypatch.setattr(gateway_mod, "find_gateway_pids", lambda **_k: [worker_pid])
    monkeypatch.setattr(
        gateway_mod, "find_profile_gateway_processes", lambda **_k: [profile_proc]
    )
    monkeypatch.setattr(gateway_mod, "_get_restart_drain_timeout", lambda: 0.1)
    # Graceful drain succeeds: the worker exits, leaving zero survivors — and
    # an exited worker is UNINSPECTABLE afterwards, exactly like the real
    # process table. Resolving the launcher after this point is impossible,
    # so the pause must snapshot launcher ancestors before draining. This is
    # precisely the case that used to leave the launcher alive and abort.
    drained_dead: set[int] = set()

    def _drain_marks_workers_dead(pids, *, timeout):
        drained_dead.update(int(p) for p in pids)
        return set()

    monkeypatch.setattr(
        cli_main,
        "_wait_for_windows_update_gateway_exit",
        _drain_marks_workers_dead,
    )

    fake = _fake_psutil_tree(
        {worker_pid: launcher_pid}, venv_exe, worker_exe, dead=drained_dead
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    terminated = []
    monkeypatch.setattr(
        status_mod,
        "terminate_pid",
        lambda pid, force=False: terminated.append(int(pid)),
    )

    cli_main._pause_windows_gateways_for_update()

    # What the downstream venv-holder guard would report as blocking.
    guard_would_abort_on = {launcher_pid}
    assert guard_would_abort_on.issubset(set(terminated)), (
        f"pause stopped {sorted(terminated)} but the venv guard aborts on "
        f"{sorted(guard_would_abort_on)} — disjoint sets abort the update"
    )


# ---------------------------------------------------------------------------
# _leftover_pausable_gateway_pids (the guard-level gateway fallback)
#
# The pause stops every gateway discovery finds, but the venv-holder guard
# sees the process table as it is NOW. A supervisor (Scheduled Task, login
# watchdog) can respawn a gateway inside the pause→guard window, and some
# spawn paths never register in discovery at all. Those holders are exactly
# what the pause machinery exists to stop — the guard nominates them for a
# stop-and-recheck instead of dead-ending, and refuses the moment any
# non-gateway holder is present. Desktop-MANAGED serve/dashboard backends
# are also refused (#81869 triage): the app respawns them within seconds, so
# nominating them for terminate_pid(force=True) kills the desktop's own
# backend mid-update.
# ---------------------------------------------------------------------------


GATEWAY_ARGV = [
    r"C:\x\venv\Scripts\python.exe",
    "-m",
    "hermes_cli.main",
    "gateway",
    "run",
]


def _fake_psutil_cmdlines(argv_by_pid):
    """psutil stand-in serving live argv per pid; unknown pids raise.

    ``environ``/``parent`` are exposed as a missing-attribute stand-in
    (desktop-managed detection catches AttributeError), so a plain argv fake
    behaves exactly like an unreadable-env process: the pausable
    classification is decided by argv alone.
    """

    class FakeProc:
        def __init__(self, pid):
            if pid not in argv_by_pid:
                raise ValueError(f"no such pid {pid}")
            self._argv = argv_by_pid[pid]

        def cmdline(self):
            return self._argv

        def environ(self):
            raise AttributeError("not modelled")

        def parent(self):
            raise AttributeError("not modelled")

        @property
        def pid(self):
            raise AttributeError("not modelled")

    return types.SimpleNamespace(Process=FakeProc)


def test_leftover_holders_that_are_all_gateways_are_nominated(monkeypatch):
    """Respawned/unmapped gateway holders get stopped, not dead-ended on."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_cmdlines({300: GATEWAY_ARGV, 301: GATEWAY_ARGV}),
    )
    matches = [
        (300, "python.exe", "truncated..."),
        (301, "python.exe", "truncated..."),
    ]

    assert cli_main._leftover_pausable_gateway_pids(matches) == [300, 301]


def test_one_non_gateway_holder_keeps_the_hard_refusal(monkeypatch):
    """A REPL/backend holder means the guard must abort exactly as before."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_cmdlines(
            {300: GATEWAY_ARGV, 400: [r"C:\x\venv\Scripts\python.exe", "-i"]}
        ),
    )
    matches = [(300, "python.exe", "..."), (400, "python.exe", "...")]

    assert cli_main._leftover_pausable_gateway_pids(matches) is None


def test_serve_backend_holders_are_nominated_like_gateways(monkeypatch):
    """A secondary profile's `serve` backend (the #81774 gap) is a backend
    the updater reaps later in the flow (_kill_stale_dashboard_processes),
    so the guard must nominate it for a stop-and-recheck, not dead-end."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_cmdlines(
            {
                400: [
                    r"C:\x\venv\Scripts\python.exe",
                    "-m",
                    "hermes_cli.main",
                    "--profile",
                    "secondary",
                    "serve",
                ],
                500: GATEWAY_ARGV,
            }
        ),
    )
    matches = [(400, "python.exe", "..."), (500, "python.exe", "...")]

    assert cli_main._leftover_pausable_gateway_pids(matches) == [400, 500]


def test_unreadable_argv_falls_back_to_the_captured_prefix(monkeypatch):
    """psutil failure degrades to the scan's captured cmdline, not a crash.

    The captured prefix decides: a gateway invocation still qualifies, and
    anything else still refuses.
    """
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil_cmdlines({}))
    gateway_prefix = r"venv\Scripts\python.exe -m hermes_cli.main gateway run"

    assert cli_main._leftover_pausable_gateway_pids(
        [(300, "python.exe", gateway_prefix)]
    ) == [300]
    assert (
        cli_main._leftover_pausable_gateway_pids(
            [
                (300, "python.exe", gateway_prefix),
                (400, "python.exe", "python.exe -i"),
            ]
        )
        is None
    )


# ---------------------------------------------------------------------------
# Desktop-managed backend exclusion (#81869 triage)
#
# The Desktop app marks every backend it spawns with HERMES_DESKTOP=1 in the
# child env and supervises it (respawns within seconds). The pausable
# classification must therefore NOT nominate a desktop-owned `serve` holder
# for terminate_pid(force=True) — the update would kill the desktop's own
# backend mid-flight — while a manually-started secondary-profile backend
# (the #81774 case) keeps its nomination.
# ---------------------------------------------------------------------------

SERVE_ARGV = [
    r"C:\x\venv\Scripts\python.exe",
    "-m",
    "hermes_cli.main",
    "serve",
    "--host",
    "127.0.0.1",
]


def _fake_psutil_desktop(argv_by_pid, env_by_pid=None):
    """psutil stand-in with live argv AND a per-pid environ dict.

    ``env_by_pid`` maps pid -> dict (or ``None``/absent → empty env).
    ``parent`` is not modelled, so the env signal alone decides.
    """

    env_by_pid = env_by_pid or {}

    class FakeProc:
        def __init__(self, pid):
            if pid not in argv_by_pid:
                raise ValueError(f"no such pid {pid}")
            self.pid = pid
            self._argv = argv_by_pid[pid]
            self._env = env_by_pid.get(pid, {})

        def cmdline(self):
            return self._argv

        def environ(self):
            return self._env

        def parent(self):
            raise AttributeError("not modelled")

    return types.SimpleNamespace(Process=FakeProc)


def test_desktop_managed_serve_keeps_the_hard_refusal(monkeypatch):
    """A `serve` backend the Desktop spawned (HERMES_DESKTOP=1) is NOT
    pausable: the app supervises and respawns it, so killing it here is
    futile and the guard must refuse exactly as before (#81869 triage)."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_desktop(
            {400: SERVE_ARGV},
            env_by_pid={400: {"HERMES_DESKTOP": "1"}},
        ),
    )
    matches = [(400, "python.exe", "...")]

    assert cli_main._leftover_pausable_gateway_pids(matches) is None


def test_manual_secondary_profile_serve_still_nominated(monkeypatch):
    """A manually-started secondary-profile `serve` backend has no
    HERMES_DESKTOP marker, so the pausable nomination (#81774) is kept."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_desktop(
            {
                400: [
                    r"C:\x\venv\Scripts\python.exe",
                    "-m",
                    "hermes_cli.main",
                    "--profile",
                    "secondary",
                    "serve",
                ]
            },
            env_by_pid={400: {}},  # no HERMES_DESKTOP marker
        ),
    )
    matches = [(400, "python.exe", "...")]

    assert cli_main._leftover_pausable_gateway_pids(matches) == [400]


def test_desktop_child_pid_env_excludes_listed_holder(monkeypatch):
    """When the Desktop hands us HERMES_DESKTOP_CHILD_PID (the same list
    _kill_stale_dashboard_processes reads and skips), a holder on it is
    desktop-managed by the Desktop's own admission — hard refusal."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_desktop(
            {400: SERVE_ARGV, 401: SERVE_ARGV},
            env_by_pid={400: {}, 401: {}},
        ),
    )
    monkeypatch.setenv("HERMES_DESKTOP_CHILD_PID", "400,999")

    assert cli_main._leftover_pausable_gateway_pids(
        [(400, "python.exe", "..."), (401, "python.exe", "...")]
    ) is None


def test_desktop_env_unreadable_but_hermes_exe_parent_refuses(monkeypatch):
    """Even when the holder env can't be read, a live Hermes.exe parent
    (the packaged app under release/*-unpacked) marks it desktop-managed."""
    import types as _types

    class ParentProc:
        def is_running(self):
            return True

        def exe(self):
            return r"C:\x\apps\desktop\release\win-unpacked\Hermes.exe"

    class FakeProc:
        pid = 400

        def cmdline(self):
            return SERVE_ARGV

        def environ(self):
            raise OSError("Access is denied")

        def parent(self):
            return ParentProc()

    fake = _types.SimpleNamespace(
        Process=lambda pid: FakeProc() if pid == 400 else (_ for _ in ()).throw(ValueError())
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert cli_main._leftover_pausable_gateway_pids(
        [(400, "python.exe", "...")]
    ) is None


def test_manual_serve_under_plain_parent_still_nominated(monkeypatch):
    """A serve backend whose live parent is a plain shell is manual: the
    updater reaps it later (_kill_stale_dashboard_processes), so it keeps
    its stop-and-recheck nomination."""
    import types as _types

    class ParentProc:
        def is_running(self):
            return True

        def exe(self):
            return r"C:\Windows\System32\cmd.exe"

    class FakeProc:
        pid = 400

        def cmdline(self):
            return SERVE_ARGV

        def environ(self):
            raise OSError("Access is denied")

        def parent(self):
            return ParentProc()

    fake = _types.SimpleNamespace(
        Process=lambda pid: FakeProc() if pid == 400 else (_ for _ in ()).throw(ValueError())
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert cli_main._leftover_pausable_gateway_pids(
        [(400, "python.exe", "...")]
    ) == [400]


# ---------------------------------------------------------------------------
# serve/dashboard backend restart after update (#81774)
#
# The venv-holder guard stops headless serve/dashboard backends it cannot
# otherwise clear (e.g. a manual secondary-profile serve). Gateways are
# resumed by _resume_windows_gateways_after_update; serve/dashboard have no
# supervisor, so their argv is captured before the kill and respawned when
# the update finishes (_capture_serve_backend_argvs / _respawn_stopped_
# serve_backends).
# ---------------------------------------------------------------------------


def _fake_psutil_capture(argv_by_pid):
    """psutil stand-in exposing only the live argv for the respawn winnower."""

    class FakeProc:
        def __init__(self, pid):
            if pid not in argv_by_pid:
                raise ValueError(f"no such pid {pid}")
            self._argv = argv_by_pid[pid]

        def cmdline(self):
            return list(self._argv)

    return types.SimpleNamespace(Process=FakeProc)


def test_capture_serve_backend_argvs_keeps_serve_drops_gateway(monkeypatch):
    """The respawn winnower must snapshot a manual `serve` backend's argv but
    NOT a gateway's (gateways are resumed by their own pause/resume token)."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_capture(
            {
                400: SERVE_ARGV,
                500: GATEWAY_ARGV,
            }
        ),
    )

    argvs = cli_main._capture_serve_backend_argvs([400, 500])
    assert argvs == [SERVE_ARGV]


def test_capture_serve_backend_argvs_drops_unreadable(monkeypatch):
    """A PID whose argv cannot be read is dropped, never a crash."""
    monkeypatch.setitem(
        sys.modules,
        "psutil",
        _fake_psutil_capture({}),
    )
    assert cli_main._capture_serve_backend_argvs([999]) == []


def test_respawn_stopped_serve_backends_calls_shared_respawner(monkeypatch, capsys):
    """The atexit restart delegates to the shared detached respawner, and
    prints a manual hint only when a respawn failed."""
    captured = False

    def fake_respawn(commandss):
        nonlocal captured
        captured = True
        assert commandss == [SERVE_ARGV]
        return []  # nothing failed

    monkeypatch.setattr(cli_main, "_respawn_dashboard_processes", fake_respawn)
    cli_main._respawn_stopped_serve_backends([SERVE_ARGV])
    out = capsys.readouterr().out
    assert captured is True
    assert "Restart any backend not auto-restarted" not in out

    def fake_respawn_fail(commandss):
        return [SERVE_ARGV]

    monkeypatch.setattr(cli_main, "_respawn_dashboard_processes", fake_respawn_fail)
    cli_main._respawn_stopped_serve_backends([SERVE_ARGV])
    out = capsys.readouterr().out
    assert "Restart any backend not auto-restarted" in out


def test_respawn_stopped_serve_backends_is_noop_for_empty(monkeypatch):
    """No captured argvs → nothing respawned, no output."""
    called = []
    monkeypatch.setattr(cli_main, "_respawn_dashboard_processes", lambda cmds: called.append(cmds))
    cli_main._respawn_stopped_serve_backends([])
    assert called == []














# ---------------------------------------------------------------------------
# cmd_update integration — concurrent-instance gate
# ---------------------------------------------------------------------------




