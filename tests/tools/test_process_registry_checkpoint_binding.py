"""The registry's reader threads must stay bound to the home they spawned under.

``ProcessRegistry`` starts one daemon thread per background process
(``_reader_loop``, ``_pty_reader_loop``, ``_env_poller_loop``). Every one of
them ends by calling ``_move_to_finished`` -> ``_write_checkpoint`` ->
``_checkpoint_path()`` -> ``atomic_json_write(<home>/processes.json)``.

``_checkpoint_path()`` resolving live is the 2026-06-11 fix for the ORIGINAL
import-time class — and that lazy resolve is exactly what creates the "resolved
TOO LATE" variant here. The thread's lifetime is bounded by the *child process*,
not by the scope that started it: under pytest a child outliving the test means
the exit tick lands after ``monkeypatch`` teardown, so the checkpoint is written
into whatever ``HERMES_HOME`` was *restored* to — the real ``~/.hermes`` — where
it clobbers the live gateway's crash-recovery state.

The rule (GBrain ``concepts/import-time-hermes-home-snapshot-bug``): resolve at
the moment the value's meaning is fixed, then CARRY it. For these threads that
moment is spawn — the process the checkpoint describes belongs to the home it
was spawned under.
"""

import threading
import time

import pytest

import tools.process_registry as pr
from tools.process_registry import ProcessRegistry, ProcessSession


def _wait_for(predicate, timeout=10.0, interval=0.02):
    """Bounded wait-until — never a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """Two homes: A is what the thread starts under, B is the restored env."""
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", None)
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    return home_a, home_b


def _session(sid="proc_bind", **kw):
    return ProcessSession(id=sid, command="sleep", started_at=time.time(), **kw)


class _FakeStdout:
    """A pipe that is already at EOF, so the reader loop falls straight through."""

    class _Buffer:
        def read1(self, _n):
            return b""

    buffer = _Buffer()

    def read(self, _n):
        return ""


class _FakeProc:
    returncode = 0
    stdout = _FakeStdout()

    def wait(self, timeout=None):
        return 0


class _FakePty:
    exitstatus = 0

    def isalive(self):
        return False

    def read(self, _n):
        return b""

    def wait(self):
        return 0


class _FakeEnv:
    """A sandbox backend whose child has already exited with status 0."""

    def execute(self, command, timeout=None):
        if command.startswith("kill -0"):
            return {"output": "1"}  # non-zero => child is gone
        return {"output": "0"} if "exit" in command else {"output": ""}


# ---------------------------------------------------------------------------
# The carry: a loop that finishes after the env moved must not follow it.
# ---------------------------------------------------------------------------


def _assert_bound(registry, session, runner, home_a, home_b, monkeypatch):
    """Run ``runner`` with HERMES_HOME flipped to B under a session bound to A."""
    session.checkpoint_path = home_a / "processes.json"
    with registry._lock:
        registry._running[session.id] = session

    # The moment monkeypatch teardown restores the env under the thread.
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive(), "reader loop never finished"

    assert not (home_b / "processes.json").exists(), (
        "the reader thread followed HERMES_HOME after the env moved — on a "
        "real run that write lands in ~/.hermes/processes.json and clobbers "
        "the gateway's crash-recovery state"
    )
    assert (home_a / "processes.json").exists(), (
        "the checkpoint was not written to the home captured at spawn"
    )


def test_reader_loop_checkpoints_into_the_home_captured_at_spawn(homes, monkeypatch):
    home_a, home_b = homes
    registry = ProcessRegistry()
    session = _session()
    session.process = _FakeProc()
    _assert_bound(
        registry, session, lambda: registry._reader_loop(session),
        home_a, home_b, monkeypatch,
    )


def test_pty_reader_loop_checkpoints_into_the_home_captured_at_spawn(homes, monkeypatch):
    home_a, home_b = homes
    registry = ProcessRegistry()
    session = _session(sid="proc_bind_pty")
    session._pty = _FakePty()
    _assert_bound(
        registry, session, lambda: registry._pty_reader_loop(session),
        home_a, home_b, monkeypatch,
    )


def test_env_poller_loop_checkpoints_into_the_home_captured_at_spawn(homes, monkeypatch):
    home_a, home_b = homes
    registry = ProcessRegistry()
    session = _session(sid="proc_bind_env")
    _assert_bound(
        registry,
        session,
        lambda: registry._env_poller_loop(
            session, _FakeEnv(), "/tmp/log", "/tmp/pid", "/tmp/exit"
        ),
        home_a, home_b, monkeypatch,
    )


# ---------------------------------------------------------------------------
# The capture: spawn must record the path, or there is nothing to carry.
# ---------------------------------------------------------------------------


def test_spawn_local_records_the_checkpoint_path_on_the_session(homes, tmp_path):
    """Capture happens at spawn — the moment the checkpoint's meaning is fixed."""
    home_a, _ = homes
    registry = ProcessRegistry()
    session = registry.spawn_local("echo bound", cwd=str(tmp_path))
    try:
        assert session.checkpoint_path == home_a / "processes.json", (
            "spawn_local did not capture the checkpoint path, so the reader "
            "thread has nothing to carry and must resolve live"
        )
    finally:
        registry.kill_process(session.id)


# ---------------------------------------------------------------------------
# The two guard rails from the fix pattern.
# ---------------------------------------------------------------------------


def test_write_checkpoint_skips_when_the_captured_home_is_gone(homes, tmp_path):
    """A thread bound to a deleted pytest tmp_path must not recreate it."""
    registry = ProcessRegistry()
    gone = tmp_path / "deleted_home"  # never created

    registry._write_checkpoint(checkpoint_path=gone / "processes.json")

    assert not gone.exists(), (
        "writer recreated a home that no longer exists — a thread bound to a "
        "deleted tmp_path must leave no litter"
    )


def test_direct_callers_still_resolve_live(homes):
    """Passing no path keeps the current, correct behaviour for live callers."""
    home_a, home_b = homes
    registry = ProcessRegistry()

    registry._write_checkpoint()
    assert (home_a / "processes.json").exists()

    # A synchronous caller in a process whose home genuinely moved follows it.
    import os

    os.environ["HERMES_HOME"] = str(home_b)
    registry._write_checkpoint()
    assert (home_b / "processes.json").exists()


def test_checkpoint_path_override_still_wins_for_tests(homes, monkeypatch, tmp_path):
    """The CHECKPOINT_PATH seam must keep working for the tests that use it."""
    pinned = tmp_path / "pinned.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", pinned)
    registry = ProcessRegistry()

    registry._write_checkpoint()

    assert pinned.exists()
