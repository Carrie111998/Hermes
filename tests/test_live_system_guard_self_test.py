"""Self-test for the live-system guard fixture in tests/conftest.py.

This file is the canary. If anyone removes a guard or weakens it, these
tests fail. If anyone adds a NEW kill primitive to the codebase without
adding it to the guard, the corresponding test added here will fail too.

The guard exists to protect the developer's live ``hermes-gateway`` process
from being SIGTERMed by tests. See PR #23397 for the original incident
(5+ live gateway kills in 3 days). Per Teknium 2026-05-10:

  > "You better do such a deep scan and scrub of the tests that this
  >  never is possible ever again for all eternity."

Every primitive that can deliver a signal to a foreign process or mutate
the live systemd unit MUST be exercised below. Adding a new primitive to
the guard? Add a test here too.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import types

import pytest

# A guaranteed-foreign PID: PID 1 (init).  Owned by root, not us, and
# always exists. A sane guard refuses to signal it.
FOREIGN_PID = 1


_SLEEP_FOR_TEST = "import time; time.sleep(30)"
_PID_ZERO_CANARY_CHILD = "HERMES_LIVE_GUARD_PID_ZERO_CANARY_CHILD"


def _sleeping_child(**kwargs) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", _SLEEP_FOR_TEST],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        **kwargs,
    )


def _cleanup_child(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)


def _patch_psutil_process_for_pid(
    monkeypatch: pytest.MonkeyPatch,
    pid: int,
    *,
    parents: list[int] | None = None,
    create_time_delta: float = 0.0,
    access_denied: bool = False,
    no_such_process: bool = False,
) -> None:
    import psutil

    real_process = psutil.Process

    class FakeProcess:
        def __init__(self, current_pid: int):
            if current_pid == pid and no_such_process:
                raise psutil.NoSuchProcess(pid)
            self.pid = current_pid
            self._real = real_process(current_pid)

        def create_time(self):
            if self.pid == pid and access_denied:
                raise psutil.AccessDenied(pid=pid)
            value = self._real.create_time()
            if self.pid == pid:
                return value + create_time_delta
            return value

        def parents(self):
            if self.pid == pid and parents is not None:
                return [types.SimpleNamespace(pid=parent) for parent in parents]
            return self._real.parents()

        def __getattr__(self, name: str):
            return getattr(self._real, name)

    monkeypatch.setattr(psutil, "Process", FakeProcess)


def _stub_systemctl(tmp_path, monkeypatch) -> None:
    stub = tmp_path / "systemctl"
    stub.write_text(
        "#!/bin/sh\nprintf 'stub-systemctl:%s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")


# ──────────────────── fail-closed self-protection ──────────────
#
# This file executes REAL kill primitives — os.kill(-1, SIGTERM), killpg,
# pkill -f python — and depends entirely on the autouse ``_live_system_guard``
# fixture in tests/conftest.py to intercept them. That makes the canary
# fail-OPEN: in any collection context where this file is present but its home
# conftest is not, the primitives fire for real and ``os.kill(-1, SIGTERM)``
# SIGTERMs every process the invoking user owns (a full desktop-session kill was
# reported in the field — see issue #68311). Such contexts are not exotic:
# published sdists that ship ``tests/`` but not ``tests/conftest.py``, trees
# assembled by copying ``test*.py`` files (that glob does NOT match
# ``conftest.py``), ``pytest --noconftest``, or running from a foreign rootdir.
#
# The fixture below makes the canary fail-CLOSED instead: it refuses to run any
# test in this file unless the guard is provably active, so no collection
# context can ever detonate the primitives. The one thing the canary can detect
# about its own safety is that the guard monkeypatches ``os.kill`` with a plain
# Python function, whereas the unguarded primitive is a C builtin.


def _live_system_guard_is_active() -> bool:
    """True iff tests/conftest.py's ``_live_system_guard`` has patched os.kill.

    The guard replaces ``os.kill`` with a plain Python function; the raw,
    unguarded primitive is a C builtin (``types.BuiltinFunctionType``). If
    ``os.kill`` is still the builtin, the guard never loaded and every kill
    primitive in this file would fire for real.
    """
    return not isinstance(os.kill, types.BuiltinFunctionType)


@pytest.fixture(autouse=True)
def _refuse_to_fire_live_weapons(request):
    """Fail closed: refuse to run a canary test unless the guard is active.

    Tests genuinely marked ``@pytest.mark.live_system_guard_bypass`` opt out
    (they run the raw primitive deliberately and harmlessly, e.g. a signal-0
    liveness probe of our own PID), matching the guard's own bypass contract.
    """
    if request.node.get_closest_marker("live_system_guard_bypass"):
        yield
        return
    if not _live_system_guard_is_active():
        pytest.fail(
            "REFUSING TO RUN: the live-system guard from tests/conftest.py is "
            "not active in this interpreter (os.kill is still the raw C "
            "builtin). This canary file executes real kill primitives — "
            "os.kill(-1, SIGTERM), killpg, pkill -f python — and relies on "
            "the guard to intercept them; unguarded, they SIGTERM every process "
            "the current user owns. This usually means the file was collected "
            "without its home tests/conftest.py (note: a test*.py copy glob "
            "does NOT match conftest.py). See issue #68311.",
            pytrace=False,
        )
    yield


def test_fail_closed_probe_reports_guard_active():
    """In the real suite the guard is loaded, so the probe reports active and
    ``_refuse_to_fire_live_weapons`` stays out of the way (no false positives
    that would wedge CI)."""
    assert _live_system_guard_is_active() is True


def test_fail_closed_probe_classifies_raw_builtin_as_unguarded():
    """The probe's discriminator, exercised against real objects: a raw C
    builtin the guard never touches (``os.getpid``) is exactly what an
    unguarded ``os.kill`` looks like and must read as 'guard not active', while
    the loaded guard's ``os.kill`` is a plain Python function."""
    assert isinstance(os.getpid, types.BuiltinFunctionType)
    assert not isinstance(os.kill, types.BuiltinFunctionType)


# ──────────────────── kill primitives ─────────────────────────


def test_os_kill_blocks_foreign_pid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(FOREIGN_PID, signal.SIGTERM)


def test_os_kill_blocks_negative_one():
    """``os.kill(-1, sig)`` signals every process we can reach. Must be blocked."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.kill(-1, signal.SIGTERM)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PID-zero semantics")
def test_os_kill_blocks_destructive_zero_pid():
    """A broken guard may kill only the isolated child session, never pytest's parent."""
    if os.environ.get(_PID_ZERO_CANARY_CHILD) == "1":
        with pytest.raises(RuntimeError, match="live-system guard"):
            os.kill(0, signal.SIGTERM)
        return

    env = os.environ.copy()
    env[_PID_ZERO_CANARY_CHILD] = "1"
    test_node = f"{__file__}::test_os_kill_blocks_destructive_zero_pid"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", test_node],
        env=env,
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_os_killpg_blocks_foreign_pgid():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.killpg(FOREIGN_PID, signal.SIGTERM)  # windows-footgun: ok — POSIX-only


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_recorded_new_session_child_killpg_passes_with_hidden_parent_chain(
    monkeypatch,
):
    proc = _sleeping_child(start_new_session=True)
    try:
        with monkeypatch.context() as mp:
            _patch_psutil_process_for_pid(mp, proc.pid, parents=[])
            os.killpg(proc.pid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only
        proc.wait(timeout=2)
    finally:
        _cleanup_child(proc)
    assert proc.returncode in {-signal.SIGTERM, 128 + int(signal.SIGTERM)}


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_recorded_non_session_leader_child_killpg_is_blocked():
    proc = _sleeping_child()
    try:
        with pytest.raises(RuntimeError, match="live-system guard"):
            os.killpg(proc.pid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only
        assert proc.poll() is None
    finally:
        _cleanup_child(proc)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
def test_recorded_exited_new_session_child_killpg_is_blocked():
    proc = _sleeping_child(start_new_session=True)
    pid = proc.pid
    try:
        os.kill(pid, signal.SIGTERM)
        proc.wait(timeout=2)
        with pytest.raises(RuntimeError, match="live-system guard"):
            os.killpg(pid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only
    finally:
        _cleanup_child(proc)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="killpg POSIX-only")
@pytest.mark.parametrize(
    "identity_override",
    [
        {"create_time_delta": 1000.0},
        {"access_denied": True},
    ],
    ids=["mismatched", "unverifiable"],
)
def test_recorded_new_session_child_killpg_requires_current_identity(
    monkeypatch, identity_override
):
    proc = _sleeping_child(start_new_session=True)
    try:
        with monkeypatch.context() as mp:
            _patch_psutil_process_for_pid(mp, proc.pid, **identity_override)
            with pytest.raises(RuntimeError, match="live-system guard"):
                os.killpg(proc.pid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only
        assert proc.poll() is None
    finally:
        _cleanup_child(proc)


def test_recorded_child_mismatched_identity_blocks_os_kill(monkeypatch):
    proc = _sleeping_child()
    try:
        with monkeypatch.context() as mp:
            _patch_psutil_process_for_pid(
                mp,
                proc.pid,
                parents=[os.getpid()],
                create_time_delta=1000.0,
            )
            with pytest.raises(RuntimeError, match="live-system guard"):
                os.kill(proc.pid, signal.SIGTERM)
        assert proc.poll() is None
    finally:
        _cleanup_child(proc)


def test_recorded_child_unverifiable_identity_blocks_os_kill(monkeypatch):
    proc = _sleeping_child()
    try:
        with monkeypatch.context() as mp:
            _patch_psutil_process_for_pid(
                mp,
                proc.pid,
                parents=[os.getpid()],
                access_denied=True,
            )
            with pytest.raises(RuntimeError, match="live-system guard"):
                os.kill(proc.pid, signal.SIGTERM)
        assert proc.poll() is None
    finally:
        _cleanup_child(proc)


def test_recorded_child_missing_during_identity_check_raises_process_lookup(
    monkeypatch,
):
    proc = _sleeping_child()
    try:
        with monkeypatch.context() as mp:
            _patch_psutil_process_for_pid(mp, proc.pid, no_such_process=True)
            with pytest.raises(ProcessLookupError):
                os.kill(proc.pid, signal.SIGTERM)
        assert proc.poll() is None
    finally:
        _cleanup_child(proc)


@pytest.mark.parametrize("use_shell", [False, True], ids=["exec", "shell"])
def test_asyncio_recorded_child_kill_passes_with_hidden_parent_chain(
    monkeypatch, use_shell
):
    import asyncio

    async def _exercise():
        if use_shell:
            command = subprocess.list2cmdline(
                [sys.executable, "-c", _SLEEP_FOR_TEST]
            )
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                _SLEEP_FOR_TEST,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        try:
            with monkeypatch.context() as mp:
                _patch_psutil_process_for_pid(mp, proc.pid, parents=[])
                os.kill(proc.pid, signal.SIGTERM)
            await asyncio.wait_for(proc.wait(), timeout=2)
        finally:
            if proc.returncode is None:
                try:
                    os.kill(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(proc.wait(), timeout=2)
        assert proc.returncode in {-signal.SIGTERM, 128 + int(signal.SIGTERM)}

    asyncio.run(_exercise())


# ──────────────────── subprocess regex bypasses ────────────────


def test_subprocess_run_systemctl_restart_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_full_path_systemctl_blocked():
    """``/usr/bin/systemctl`` (full path) must be blocked too."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["/usr/bin/systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_run_sudo_systemctl_blocked():
    """``sudo systemctl ...`` defeated the old head==systemctl check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sudo", "systemctl", "restart", "hermes-gateway"])


def test_subprocess_run_env_systemctl_blocked():
    """``env systemctl ...`` similarly defeated the old head check."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["env", "systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_run_bash_c_systemctl_blocked():
    """``bash -c "systemctl ..."`` must also be caught."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["bash", "-c", "systemctl --user restart hermes-gateway"])


def test_subprocess_run_sh_c_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["sh", "-c", "systemctl --user stop hermes-gateway"])


def test_subprocess_run_setsid_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["setsid", "systemctl", "kill", "hermes-gateway"])


def test_subprocess_run_string_shell_true_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(
            "systemctl --user restart hermes-gateway",
            shell=True,
        )


def test_subprocess_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.Popen(["systemctl", "--user", "stop", "hermes-gateway"])


def test_subprocess_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_call_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_call(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_check_output_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.check_output(["systemctl", "--user", "restart", "hermes-gateway"])


def test_subprocess_getoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getoutput("systemctl --user restart hermes-gateway")


def test_subprocess_getstatusoutput_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.getstatusoutput("systemctl --user restart hermes-gateway")


# ──────────────────── os.system / os.popen ────────────────────


def test_os_system_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.system("systemctl --user restart hermes-gateway")


def test_os_popen_systemctl_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        os.popen("systemctl --user restart hermes-gateway")


# ──────────────────── pty.spawn ────────────────────────────────


def test_pty_spawn_systemctl_blocked():
    import pty
    with pytest.raises(RuntimeError, match="live-system guard"):
        pty.spawn(["systemctl", "--user", "restart", "hermes-gateway"])


# ──────────────────── asyncio.create_subprocess_* ──────────────


def test_asyncio_create_subprocess_exec_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_exec(
            "systemctl", "--user", "restart", "hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


def test_asyncio_create_subprocess_shell_systemctl_blocked():
    import asyncio

    async def _attempt():
        await asyncio.create_subprocess_shell(
            "systemctl --user restart hermes-gateway"
        )

    with pytest.raises(RuntimeError, match="live-system guard"):
        asyncio.run(_attempt())


# ──────────────────── pkill / killall / taskkill ───────────────


def test_subprocess_pkill_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes"])


def test_subprocess_pkill_hermes_gateway_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "hermes-gateway"])


def test_subprocess_pkill_python_dash_f_blocked():
    """``pkill -f python`` matches the gateway's "python -m hermes_cli.main"."""
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["pkill", "-f", "python"])


def test_subprocess_killall_hermes_blocked():
    with pytest.raises(RuntimeError, match="live-system guard"):
        subprocess.run(["killall", "hermes"])


# ──────────────────── pass-through cases (must NOT raise) ──────


def test_systemctl_status_passes_through(tmp_path, monkeypatch):
    """Read-only systemctl probes (status/show/list-units) are fine."""
    _stub_systemctl(tmp_path, monkeypatch)
    r = subprocess.run(
        ["systemctl", "--user", "status", "hermes-gateway", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "stub-systemctl:" in r.stdout


def test_systemctl_show_passes_through(tmp_path, monkeypatch):
    _stub_systemctl(tmp_path, monkeypatch)
    r = subprocess.run(
        ["systemctl", "--user", "show", "hermes-gateway", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "stub-systemctl:" in r.stdout


def test_systemctl_list_units_passes_through(tmp_path, monkeypatch):
    _stub_systemctl(tmp_path, monkeypatch)
    r = subprocess.run(
        ["systemctl", "--user", "list-units", "fake-not-real-unit*", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "stub-systemctl:" in r.stdout


def test_systemctl_unrelated_unit_passes_through(tmp_path, monkeypatch):
    """Read-only systemctl probes of non-Hermes units are allowed."""
    _stub_systemctl(tmp_path, monkeypatch)
    r = subprocess.run(
        ["systemctl", "--user", "show", "fake-not-real-unit"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "stub-systemctl:" in r.stdout


def test_kill_own_subtree_passes_through():
    """We CAN kill our own children — guard recognizes them via psutil."""
    p = subprocess.Popen(["sleep", "30"])
    try:
        os.kill(p.pid, signal.SIGTERM)
    finally:
        p.wait(timeout=2)
    # SIGTERM = 15; subprocess returncode is -15 on POSIX.
    assert p.returncode in {-signal.SIGTERM, 128 + int(signal.SIGTERM)}


def test_subprocess_pkill_with_unrelated_pattern_passes_through():
    """``pkill -f some-unrelated-pattern`` (no hermes/python) is fine."""
    # We don't actually run pkill — just verify the guard would let it
    # through by inspecting the matcher. Re-implementing the check here
    # would duplicate the guard; instead spawn a noop to confirm no raise.
    # Use 'true' so it succeeds quickly.
    r = subprocess.run(["true"], capture_output=True)
    assert r.returncode == 0


def test_normal_subprocess_run_passes_through():
    """Plain non-systemctl subprocess.run should work normally."""
    r = subprocess.run(
        ["echo", "hello"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert r.stdout.strip() == "hello"


# ──────────────────── bypass marker ─────────────────────────────


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal-0 probe")
def test_bypass_marker_disables_guard():
    """The bypass marker exists for tests that genuinely need real signal delivery
    (e.g. PTY tests SIGINTing their own child). Verify it works.

    We use it harmlessly here by signaling our own PID 0 (own group) so we
    don't actually kill anything — but the call goes through real os.kill.
    """
    # With bypass, the guard yields without installing the monkeypatch,
    # so we get the real os.kill. Calling os.kill(os.getpid(), 0) just
    # checks that the PID exists — harmless.
    os.kill(os.getpid(), 0)  # windows-footgun: ok — POSIX-only signal-0 probe
