"""In-container process-group teardown for the Docker backend (issue #84967).

``docker exec`` only reports the host-side client PID, so the inherited
``BaseEnvironment._kill_process`` leaves the exec'd shell and every descendant
running inside the container. These tests pin the two halves of the fix: each
exec is launched in its own session with its PGID recorded, and teardown
signals that group from inside the container.

The mock-based tests run everywhere. The tests that exercise the kill script
itself need a POSIX shell and real process groups, so they skip on Windows —
they need no Docker daemon.
"""

import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from tools.environments import docker as docker_env

_IS_POSIX = os.name == "posix"
_HAS_SH = shutil.which("sh") is not None
_HAS_SETSID = shutil.which("setsid") is not None

requires_posix_shell = pytest.mark.skipif(
    not (_IS_POSIX and _HAS_SH),
    reason="needs a POSIX shell and real process groups",
)


class _FakeProc:
    """Minimal stand-in for the Popen returned by ``_popen_bash``."""

    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


def _bare_env(monkeypatch, *, setsid_ok=True, container_id="cid1234567890"):
    """A DockerEnvironment with only the attributes _run_bash touches.

    ``__init__`` starts a container, so the instance is built directly. The
    setsid probe is pre-seeded; tests that exercise the probe clear it.
    """
    env = object.__new__(docker_env.DockerEnvironment)
    env._docker_exe = "docker"
    env._container_id = container_id
    env._init_env_args = []
    env._profile_scoped_passthrough = False
    if setsid_ok is not None:
        env._setsid_ok = setsid_ok
    return env


def _capture_popen(monkeypatch):
    """Intercept _popen_bash, returning the list of argv lists it received."""
    calls = []

    def _fake_popen(cmd, stdin_data=None, **kwargs):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(docker_env, "_popen_bash", _fake_popen)
    return calls


def _capture_run(monkeypatch, returncode=0, raises=None):
    """Intercept subprocess.run inside docker.py."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="")

    monkeypatch.setattr(docker_env.subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# Launch side: the exec must get its own session, and record its PGID
# ---------------------------------------------------------------------------

def test_exec_runs_under_setsid_and_records_its_pgid(monkeypatch):
    """Wrap each exec in ``setsid -w`` and have it write its own PGID."""
    env = _bare_env(monkeypatch, setsid_ok=True)
    calls = _capture_popen(monkeypatch)

    proc = env._run_bash("echo hi")

    argv = calls[0]
    assert "setsid" in argv, "the command must get its own session"
    # -w is what makes setsid wait for the child it forks; without it the
    # client returns early and the caller reads a truncated result.
    assert argv[argv.index("setsid") + 1] == "-w"
    assert argv[argv.index("setsid") + 2:argv.index("setsid") + 4] == ["bash", "-c"]

    pgid_file = proc._hermes_pgid_file
    assert pgid_file.startswith(docker_env._PGID_FILE_PREFIX)

    script = argv[-1]
    assert f"echo $$ >{pgid_file}" in script, "the session leader's PID is the PGID"
    assert script.rstrip().endswith("echo hi"), "the user command must still run last"


def test_pgid_file_is_cleared_on_normal_exit(monkeypatch):
    """Trap EXIT so a completed command does not leave a file in /tmp."""
    env = _bare_env(monkeypatch, setsid_ok=True)
    calls = _capture_popen(monkeypatch)

    proc = env._run_bash("true")

    script = calls[0][-1]
    assert script.startswith("trap "), "the trap must be installed before anything can exit"
    assert f"rm -f {proc._hermes_pgid_file}" in script


def test_each_exec_gets_its_own_pgid_file(monkeypatch):
    """Concurrent commands in a shared container must not collide."""
    env = _bare_env(monkeypatch, setsid_ok=True)
    _capture_popen(monkeypatch)

    first = env._run_bash("sleep 1")
    second = env._run_bash("sleep 1")

    assert first._hermes_pgid_file != second._hermes_pgid_file


def test_login_shell_keeps_its_l_flag_under_setsid(monkeypatch):
    """Wrapping must not silently drop the login shell."""
    env = _bare_env(monkeypatch, setsid_ok=True)
    calls = _capture_popen(monkeypatch)

    env._run_bash("echo hi", login=True)

    argv = calls[0]
    assert argv[argv.index("setsid") + 2:argv.index("setsid") + 5] == ["bash", "-l", "-c"]


def test_missing_setsid_leaves_the_command_untouched(monkeypatch):
    """Degrade to the previous behaviour on images without setsid."""
    env = _bare_env(monkeypatch, setsid_ok=False)
    calls = _capture_popen(monkeypatch)

    proc = env._run_bash("echo hi")

    argv = calls[0]
    assert "setsid" not in argv
    assert argv[-3:] == ["bash", "-c", "echo hi"], "the command must not be rewritten"
    assert getattr(proc, "_hermes_pgid_file", "") == "", \
        "nothing recorded means nothing for teardown to sweep"


def test_a_handle_that_rejects_attributes_still_executes(monkeypatch):
    """Teardown bookkeeping must never break the execution path.

    ``object()`` and any slotted or wrapped handle refuses new attributes.
    Recording the PGID file is an optimisation for the timeout path; failing
    to record it may cost the in-container sweep, never the command.
    """
    env = _bare_env(monkeypatch, setsid_ok=True)
    monkeypatch.setattr(
        docker_env, "_popen_bash", lambda cmd, stdin_data=None: object(),
    )

    proc = env._run_bash("echo hi")  # must not raise

    assert getattr(proc, "_hermes_pgid_file", "") == ""


# ---------------------------------------------------------------------------
# The setsid probe
# ---------------------------------------------------------------------------

def test_setsid_probe_runs_once_per_container(monkeypatch):
    """The probe costs an exec, so its result is cached."""
    env = _bare_env(monkeypatch, setsid_ok=None)
    _capture_popen(monkeypatch)
    runs = _capture_run(monkeypatch, returncode=0)

    env._run_bash("a")
    env._run_bash("b")

    probes = [c for c in runs if "setsid" in c]
    assert len(probes) == 1, f"probed {len(probes)} times: {probes}"
    assert probes[0][-3:] == ["setsid", "-w", "true"]


def test_setsid_probe_failure_disables_wrapping(monkeypatch):
    """A non-zero probe means busybox setsid (no -w) or none at all."""
    env = _bare_env(monkeypatch, setsid_ok=None)
    calls = _capture_popen(monkeypatch)
    _capture_run(monkeypatch, returncode=1)

    proc = env._run_bash("echo hi")

    assert "setsid" not in calls[0]
    assert getattr(proc, "_hermes_pgid_file", "") == ""


def test_setsid_probe_survives_a_dead_daemon(monkeypatch):
    """An unreachable daemon must not propagate out of the probe."""
    env = _bare_env(monkeypatch, setsid_ok=None)
    calls = _capture_popen(monkeypatch)
    _capture_run(monkeypatch, raises=OSError("daemon down"))

    proc = env._run_bash("echo hi")

    assert getattr(proc, "_hermes_pgid_file", "") == ""
    assert calls[0][-3:] == ["bash", "-c", "echo hi"]


# ---------------------------------------------------------------------------
# Teardown side
# ---------------------------------------------------------------------------

def test_kill_process_signals_the_container_group(monkeypatch):
    """Teardown must reach inside the container, not just kill the client."""
    env = _bare_env(monkeypatch)
    runs = _capture_run(monkeypatch)
    proc = _FakeProc()
    proc._hermes_pgid_file = "/tmp/.hermes-exec-pgid-abc"

    env._kill_process(proc)

    assert proc.killed, "the host-side docker exec client must still be killed"
    assert len(runs) == 1, "exactly one teardown exec"
    argv = runs[0]
    assert argv[:5] == ["docker", "exec", "cid1234567890", "sh", "-c"]
    # sh -c <script> <argv0> <arg1>: the file arrives as $1, never interpolated
    # into the script, so a path with a space or a quote cannot break out.
    assert argv[-1] == "/tmp/.hermes-exec-pgid-abc"
    script = argv[5]
    assert "kill -TERM -" in script
    assert "kill -KILL -" in script, "TERM must escalate to KILL"


def test_kill_script_avoids_syntax_dash_builtin_kill_rejects():
    """dash's builtin kill supports neither ``--`` nor ``-s``.

    On Debian-family images /bin/sh is dash, where ``kill -TERM -- -123`` and
    ``kill -s TERM -123`` both fail with "Illegal number: -" and signal
    nothing. The 2>/dev/null in the script makes that failure silent, so this
    is pinned rather than left to be "tidied up" back into a leak.
    """
    script = docker_env._TREE_KILL_SCRIPT
    assert "kill -TERM -- " not in script
    assert "kill -KILL -- " not in script
    assert "kill -0 -- " not in script
    assert "kill -s " not in script


def test_kill_process_without_a_recorded_pgid_only_kills_the_client(monkeypatch):
    """No recorded session (setsid missing) means no teardown exec to run."""
    env = _bare_env(monkeypatch)
    runs = _capture_run(monkeypatch)
    proc = _FakeProc()
    proc._hermes_pgid_file = ""

    env._kill_process(proc)

    assert proc.killed
    assert runs == []


def test_kill_process_handles_a_process_that_is_already_gone(monkeypatch):
    """A dead client must not stop the in-container sweep."""
    env = _bare_env(monkeypatch)
    runs = _capture_run(monkeypatch)

    class _GoneProc(_FakeProc):
        def kill(self):
            raise ProcessLookupError("already reaped")

    proc = _GoneProc()
    proc._hermes_pgid_file = "/tmp/.hermes-exec-pgid-abc"

    env._kill_process(proc)

    assert len(runs) == 1, "the container tree outlives the client, so still sweep"


@pytest.mark.parametrize("failure", [
    OSError("no docker"),
    subprocess.TimeoutExpired(cmd="docker", timeout=1),
])
def test_teardown_never_raises(monkeypatch, failure):
    """Teardown runs on the timeout path; raising would mask the real error."""
    env = _bare_env(monkeypatch)
    _capture_run(monkeypatch, raises=failure)
    proc = _FakeProc()
    proc._hermes_pgid_file = "/tmp/.hermes-exec-pgid-abc"

    env._kill_process(proc)  # must not raise

    assert proc.killed


def test_teardown_skipped_when_the_container_is_gone(monkeypatch):
    """The container-gone recovery path owns this case, not teardown."""
    env = _bare_env(monkeypatch, container_id="")
    runs = _capture_run(monkeypatch)
    proc = _FakeProc()
    proc._hermes_pgid_file = "/tmp/.hermes-exec-pgid-abc"

    env._kill_process(proc)

    assert runs == []


# ---------------------------------------------------------------------------
# The kill script itself, against real process groups
# ---------------------------------------------------------------------------

def _run_script(pgid_file):
    return subprocess.run(
        ["sh", "-c", docker_env._TREE_KILL_SCRIPT, "sh", str(pgid_file)],
        capture_output=True, text=True, timeout=30,
    )


@requires_posix_shell
def test_script_exits_clean_when_the_file_is_missing(tmp_path):
    """A command that exited before writing must not fail teardown."""
    result = _run_script(tmp_path / "nope")
    assert result.returncode == 0


@requires_posix_shell
@pytest.mark.parametrize("content", ["", "0", "not-a-pid", "123abc", "-1"])
def test_script_refuses_anything_that_is_not_a_pid(tmp_path, content):
    """``kill -- -0`` would signal teardown's own group. Reject non-PIDs."""
    pgid_file = tmp_path / "pgid"
    pgid_file.write_text(content)

    result = _run_script(pgid_file)

    assert result.returncode == 0
    assert result.stderr == ""


@requires_posix_shell
def test_script_removes_the_file_it_consumed(tmp_path):
    """Teardown owns cleanup for the killed case, where the trap never ran."""
    pgid_file = tmp_path / "pgid"
    pgid_file.write_text("0")

    _run_script(pgid_file)

    assert not pgid_file.exists()


@pytest.mark.skipif(
    not (_IS_POSIX and _HAS_SH and _HAS_SETSID),
    reason="needs setsid and real process groups",
)
def test_script_kills_the_whole_group_not_just_the_leader(tmp_path):
    """The regression: descendants of the exec'd shell must die with it.

    Mirrors what the container sees — a session leader with a child that
    outlives it — without needing a Docker daemon.
    """
    pgid_file = tmp_path / "pgid"
    marker = tmp_path / "child-alive"

    leader = subprocess.Popen(
        ["setsid", "sh", "-c",
         f"echo $$ >{pgid_file}; sh -c 'while :; do sleep 0.2; done' & sleep 60"],
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pgid_file.exists():
            time.sleep(0.05)
        assert pgid_file.exists(), "the leader never recorded its PGID"
        pgid = int(pgid_file.read_text().strip())

        # Sanity: the group is alive before teardown, otherwise this proves nothing.
        os.killpg(pgid, 0)

        result = _run_script(pgid_file)
        assert result.returncode == 0

        deadline = time.monotonic() + 10
        alive = True
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive, "the background child survived the group kill"
    finally:
        try:
            os.killpg(os.getpgid(leader.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            leader.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


@pytest.mark.skipif(
    not (_IS_POSIX and _HAS_SH and _HAS_SETSID),
    reason="needs setsid and real process groups",
)
def test_script_leaves_other_sessions_alone(tmp_path):
    """A shared persistent container runs concurrent commands. Stay scoped."""
    pgid_file = tmp_path / "pgid"

    victim = subprocess.Popen(["setsid", "sh", "-c", f"echo $$ >{pgid_file}; sleep 60"])
    bystander = subprocess.Popen(["setsid", "sh", "-c", "sleep 60"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pgid_file.exists():
            time.sleep(0.05)
        assert pgid_file.exists()

        _run_script(pgid_file)
        time.sleep(0.5)

        assert bystander.poll() is None, "an unrelated session was killed"
    finally:
        for proc in (victim, bystander):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
