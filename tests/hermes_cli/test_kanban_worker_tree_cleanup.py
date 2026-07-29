"""Regression tests for Kanban worker ownership and workspace exclusivity.

These tests exercise real subprocesses because a PID-only mock cannot prove that
an independently-created session/PGID descendant is cleaned up or that a reused
PID is rejected safely.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


pytestmark = pytest.mark.live_system_guard_bypass


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as connection:
        yield connection


def _wait_for_pid_file(path: Path) -> int:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if value:
                return int(value)
        time.sleep(0.01)
    raise AssertionError("worker did not publish its descendant PID")


def _local_pid_alive(pid: int) -> bool:
    """Avoid importing the gateway stack just to probe a test subprocess."""
    try:
        state = Path(f"/proc/{int(pid)}/stat").read_text(encoding="ascii")
        return state.rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _spawn_worker_with_new_session_descendant(pid_file: Path) -> subprocess.Popen:
    script = (
        "import os, pathlib, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        f"    pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(30)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_worker_that_leaves_same_session_descendant(
    pid_file: Path, release_file: Path,
) -> subprocess.Popen:
    script = (
        "import os, pathlib, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        f"    pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "    while True:\n"
        f"        if pathlib.Path({str(release_file)!r}).exists(): break\n"
        "        time.sleep(0.01)\n"
        "    time.sleep(30)\n"
        "else:\n"
        f"    while not pathlib.Path({str(release_file)!r}).exists():\n"
        "        time.sleep(0.01)\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_reclaim_terminates_descendant_that_created_its_own_session(
    kanban_home, monkeypatch, tmp_path
):
    """Reclaim must terminate the whole owned run tree, not only its leader."""
    if os.name == "nt":
        pytest.skip("process-tree test uses POSIX fork/setsid")

    pid_file = tmp_path / "descendant.pid"
    worker = _spawn_worker_with_new_session_descendant(pid_file)
    monkeypatch.setattr(kb, "_pid_alive", _local_pid_alive)
    descendant_pid = _wait_for_pid_file(pid_file)
    try:
        result = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
        )
        assert result["terminated"] is True
        assert result["descendant_pids"] == [descendant_pid]
        assert not kb._pid_alive(descendant_pid)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()


def test_reclaim_terminates_session_descendant_after_worker_exits(tmp_path):
    """A dead leader must not leave its original worker session running."""
    if os.name == "nt":
        pytest.skip("process-tree test uses POSIX fork")

    pid_file = tmp_path / "descendant.pid"
    release_file = tmp_path / "release"
    worker = _spawn_worker_that_leaves_same_session_descendant(
        pid_file, release_file,
    )
    descendant_pid = _wait_for_pid_file(pid_file)
    identity = kb._process_identity(worker.pid)
    assert identity is not None
    try:
        release_file.write_text("exit", encoding="ascii")
        assert worker.wait(timeout=5) == 0

        result = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
            expected_start_time=str(identity["start_time"]),
            expected_pgid=int(identity["pgid"]),
            expected_sid=int(identity["sid"]),
            identity_required=True,
        )
        assert result["terminated"] is True
        assert result["descendant_pids"] == [descendant_pid]
        assert not _local_pid_alive(descendant_pid)
    finally:
        if _local_pid_alive(descendant_pid):
            os.kill(descendant_pid, 9)


def test_crash_recovery_terminates_session_descendant_after_worker_exits(
    conn, tmp_path, monkeypatch,
):
    """Crash recovery must clean up descendants before making a retry ready."""
    if os.name == "nt":
        pytest.skip("process-tree test uses POSIX fork")

    pid_file = tmp_path / "crashed-descendant.pid"
    release_file = tmp_path / "crashed-release"
    worker = _spawn_worker_that_leaves_same_session_descendant(
        pid_file, release_file,
    )
    descendant_pid = _wait_for_pid_file(pid_file)
    try:
        task_id = kb.create_task(conn, title="crashed worker", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, task_id, worker.pid)
        release_file.write_text("exit", encoding="ascii")
        assert worker.wait(timeout=5) == 0

        monkeypatch.setattr(kb, "_pid_alive", _local_pid_alive)
        assert kb.detect_crashed_workers(conn) == [task_id]
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        assert not _local_pid_alive(descendant_pid)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if _local_pid_alive(descendant_pid):
            os.kill(descendant_pid, 9)


def test_reclaim_refuses_pid_reuse_when_run_identity_does_not_match(tmp_path):
    """A reused PID must not receive a signal when its start identity differs."""
    if os.name == "nt":
        pytest.skip("process identity probe is POSIX-only")

    worker = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = kb._process_identity(worker.pid)
        assert identity is not None
        result = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
            expected_start_time=str(int(identity["start_time"]) + 1),
        )
        assert result["ownership_verified"] is False
        assert result["termination_attempted"] is False
        assert worker.poll() is None
    finally:
        worker.terminate()
        worker.wait()


def test_stale_claim_does_not_extend_reused_pid(conn, tmp_path, monkeypatch):
    """A live PID with a different run identity must be released, not signalled."""
    if os.name == "nt":
        pytest.skip("process identity probe is POSIX-only")

    worker = subprocess.Popen(
        ["sleep", "30"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        task_id = kb.create_task(conn, title="reused pid", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, task_id, worker.pid)
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (int(time.time()) - 60, task_id),
        )

        original_identity = kb._process_identity(worker.pid)
        assert original_identity is not None
        reused_identity = dict(original_identity)
        reused_identity["start_time"] = str(int(original_identity["start_time"]) + 1)
        monkeypatch.setattr(kb, "_process_identity", lambda _pid: reused_identity)
        killed: list[int] = []

        assert kb.release_stale_claims(
            conn, signal_fn=lambda _pid, sig: killed.append(sig),
        ) == 1
        assert kb.get_task(conn, task_id).status == "ready"
        assert killed == []
    finally:
        worker.terminate()
        worker.wait()


def test_legacy_pid_without_identity_fails_closed(tmp_path):
    """A legacy PID-only run must not signal an unverified live process."""
    if os.name == "nt":
        pytest.skip("process identity test uses POSIX process metadata")

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        result = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
            identity_required=True,
        )
        assert result["ownership_unverified_alive"] is True
        assert result["termination_attempted"] is False
        assert worker.poll() is None

        identity = kb._process_identity(worker.pid)
        assert identity is not None
        partial = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
            expected_start_time=str(identity["start_time"]),
            identity_required=True,
        )
        assert partial["ownership_unverified_alive"] is True
        assert partial["termination_attempted"] is False
        assert worker.poll() is None
    finally:
        worker.terminate()
        worker.wait()


@pytest.mark.parametrize("missing", ["start_time", "pgid", "sid"])
def test_partial_run_identity_fails_closed_for_live_pid(tmp_path, missing):
    """Every incomplete identity tuple must remain unverified and unsignalled."""
    if os.name == "nt":
        pytest.skip("process identity test uses POSIX process metadata")

    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        identity = kb._process_identity(worker.pid)
        assert identity is not None
        expected = {
            "expected_start_time": str(identity["start_time"]),
            "expected_pgid": int(identity["pgid"]),
            "expected_sid": int(identity["sid"]),
        }
        expected[f"expected_{missing}"] = None
        killed: list[tuple[int, int]] = []
        result = kb._terminate_reclaimed_worker(
            worker.pid,
            f"{kb._claimer_id().split(':', 1)[0]}:run",
            signal_fn=lambda pid, sig: killed.append((pid, sig)),
            identity_required=True,
            **expected,
        )
        assert result["ownership_unverified_alive"] is True
        assert result["termination_attempted"] is False
        assert killed == []
        assert worker.poll() is None
    finally:
        worker.terminate()
        worker.wait()


def test_completed_worker_cleanup_excludes_root_pid(monkeypatch):
    """Completion cleanup can signal descendants without self-terminating."""
    root_pid = 1234
    child_pid = 5678
    identity = {"start_time": "42", "pgid": root_pid, "sid": root_pid}
    alive = {child_pid}
    signalled: list[int] = []

    def owned_tree(_pid, **kwargs):
        snapshot = kwargs.get("identity_snapshot")
        if snapshot is not None:
            snapshot.update({root_pid: dict(identity), child_pid: dict(identity)})
        return [root_pid, child_pid], identity

    monkeypatch.setattr(kb, "_owned_process_tree", owned_tree)
    monkeypatch.setattr(kb, "_process_identity", lambda _pid: identity)
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid in alive)

    def signal(pid, _sig):
        signalled.append(pid)
        alive.discard(pid)

    result = kb._terminate_reclaimed_worker(
        root_pid,
        f"{kb._claimer_id().split(':', 1)[0]}:run",
        signal_fn=signal,
        expected_start_time="42",
        expected_pgid=root_pid,
        expected_sid=root_pid,
        identity_required=True,
        exclude_pid=root_pid,
    )
    assert signalled == [child_pid]
    assert result["descendant_pids"] == [child_pid]
    assert result["terminated"] is True


def test_claim_rejects_second_running_writer_for_same_workspace(conn, tmp_path):
    """Two running Kanban tasks must not share an explicit directory workspace."""
    workspace = tmp_path / "shared-writer-workspace"
    first = kb.create_task(
        conn,
        title="first writer",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )
    second = kb.create_task(
        conn,
        title="second writer",
        workspace_kind="dir",
        workspace_path=str(workspace),
    )

    assert kb.claim_task(conn, first) is not None
    assert kb.claim_task(conn, second) is None
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (second,)
    ).fetchone()
    assert row["status"] == "ready"


def test_claim_rejects_concurrent_cross_connection_workspace_writer(
    conn, tmp_path
):
    """The workspace CAS remains exclusive across independent DB connections."""
    workspace = tmp_path / "shared-cross-connection-workspace"
    task_ids = [
        kb.create_task(
            conn,
            title=f"writer {index}",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        for index in range(2)
    ]
    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def claim(index: int) -> None:
        with kb.connect() as other_conn:
            barrier.wait()
            results[index] = kb.claim_task(other_conn, task_ids[index])

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sum(result is not None for result in results) == 1
    statuses = {
        row["id"]: row["status"]
        for row in conn.execute(
            "SELECT id, status FROM tasks WHERE id IN (?, ?)", task_ids
        )
    }
    assert sorted(statuses.values()) == ["ready", "running"]


def test_successful_completion_cleans_worker_descendants(monkeypatch, conn):
    """Completion snapshots identity before closing the active run."""
    task_id = kb.create_task(conn, title="successful worker", assignee="worker")
    host = kb._claimer_id().split(":", 1)[0]
    run = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
    assert run is not None
    kb._set_worker_pid(conn, task_id, os.getpid())
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0]
    assert run_id is not None

    calls: list[dict] = []

    def capture_cleanup(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(kb, "_cleanup_worker_processes", capture_cleanup)

    assert kb.complete_task(conn, task_id, summary="finished", expected_run_id=run_id)
    assert len(calls) == 1
    assert calls[0]["pid"] == os.getpid()
    assert calls[0]["claim_lock"] == run.claim_lock
    assert calls[0]["expected_start_time"] is not None
    assert calls[0]["expected_pgid"] is not None
    assert calls[0]["expected_sid"] is not None
    assert calls[0]["exclude_pid"] == os.getpid()


def test_successful_completion_terminates_dead_worker_session_descendant(
    conn, tmp_path, monkeypatch,
):
    """Completion cleans a descendant even after the worker leader exits."""
    if os.name == "nt":
        pytest.skip("process-tree test uses POSIX fork")

    pid_file = tmp_path / "completed-descendant.pid"
    release_file = tmp_path / "completed-release"
    worker = _spawn_worker_that_leaves_same_session_descendant(
        pid_file, release_file,
    )
    descendant_pid = _wait_for_pid_file(pid_file)
    try:
        identity = kb._process_identity(worker.pid)
        assert identity is not None
        task_id = kb.create_task(conn, title="completed worker", assignee="worker")
        host = kb._claimer_id().split(":", 1)[0]
        assert kb.claim_task(conn, task_id, claimer=f"{host}:worker") is not None
        kb._set_worker_pid(conn, task_id, worker.pid)
        run_id = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
        assert run_id is not None

        release_file.write_text("exit", encoding="ascii")
        assert worker.wait(timeout=5) == 0
        monkeypatch.setattr(kb, "_pid_alive", _local_pid_alive)

        assert kb.complete_task(
            conn, task_id, summary="finished", expected_run_id=run_id,
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "done"
        assert not _local_pid_alive(descendant_pid)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if _local_pid_alive(descendant_pid):
            os.kill(descendant_pid, 9)


def test_inconsistent_isolated_identity_fallback_fails_closed(monkeypatch):
    """Dead-leader fallback requires a semantically isolated PGID/SID tuple."""
    if os.name == "nt":
        pytest.skip("process identity probe is POSIX-only")
    root_pid = 999999
    host = kb._claimer_id().split(":", 1)[0]
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(kb, "_process_identity", lambda _pid: None)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)

    result = kb._terminate_reclaimed_worker(
        root_pid,
        f"{host}:worker",
        signal_fn=lambda pid, sig: signalled.append((pid, sig)),
        expected_start_time="dead",
        expected_pgid=root_pid - 1,
        expected_sid=root_pid,
        identity_required=True,
    )

    assert signalled == []
    assert result["ownership_verified"] is False
    assert result["termination_attempted"] is False


def test_pidfd_validates_identity_after_open_before_signal(monkeypatch):
    """PID reuse after pidfd_open fails closed; a matching handle is signalled."""
    if sys.platform != "linux":
        pytest.skip("pidfd is a Linux primitive")
    import signal

    recorded = {"start_time": "10", "pgid": 4321, "sid": 4321}
    current = {"start_time": "11", "pgid": 4321, "sid": 4321}
    calls: list[tuple] = []
    monkeypatch.setattr(os, "pidfd_open", lambda pid, flags: calls.append(("open", pid, flags)) or 77, raising=False)
    monkeypatch.setattr(os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(
        signal, "pidfd_send_signal",
        lambda fd, sig, info, flags: calls.append(("send", fd, sig, flags)),
        raising=False,
    )
    monkeypatch.setattr(kb, "_process_identity", lambda _pid: dict(current))

    kb._signal_verified_target(4321, signal.SIGTERM, recorded)
    assert [item[0] for item in calls] == ["open", "close"]

    calls.clear()
    current["start_time"] = "10"
    kb._signal_verified_target(4321, signal.SIGTERM, recorded)
    assert [item[0] for item in calls] == ["open", "send", "close"]


def test_missing_identity_is_unknown_not_a_match(tmp_path):
    assert kb._worker_identity_matches(1) is False


def test_administrative_completion_never_signals_registered_worker_root(
    conn, monkeypatch,
):
    """Completion cleanup excludes the registered root for every caller."""
    task_id = kb.create_task(conn, title="admin completion", assignee="worker")
    host = kb._claimer_id().split(":", 1)[0]
    run = kb.claim_task(conn, task_id, claimer=f"{host}:worker")
    assert run is not None
    registered_pid = os.getpid() + 10000
    kb._set_worker_pid(conn, task_id, registered_pid)
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()[0]
    calls = []
    monkeypatch.setattr(kb, "_cleanup_worker_processes", lambda **kw: calls.append(kw))
    assert kb.complete_task(conn, task_id, expected_run_id=run_id)
    assert calls[0]["exclude_pid"] == registered_pid
    assert kb.reconcile_terminal_worker_cleanups(conn) == []
    assert calls[1]["exclude_pid"] == registered_pid


def test_completion_keeps_workspace_lease_until_dispatcher_verifies_exit(
    conn, tmp_path, monkeypatch,
):
    """Done stays leased until a later dispatcher reconciliation proves exit."""
    workspace = tmp_path / "leased"
    first = kb.create_task(
        conn, title="first", workspace_kind="dir", workspace_path=str(workspace),
    )
    second = kb.create_task(
        conn, title="second", workspace_kind="dir", workspace_path=str(workspace),
    )
    assert kb.claim_task(conn, first) is not None
    kb._set_worker_pid(conn, first, os.getpid())
    run_id = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", (first,),
    ).fetchone()[0]
    monkeypatch.setattr(
        kb, "_cleanup_worker_processes",
        lambda **_kw: {"terminated": True, "ownership_verified": True},
    )

    assert kb.complete_task(conn, first, expected_run_id=run_id)
    cleanup_verified = conn.execute(
        "SELECT cleanup_verified FROM task_runs WHERE id = ?", (run_id,),
    ).fetchone()[0]
    assert cleanup_verified == 0
    assert kb.claim_task(conn, second) is None

    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET claim_expires = ?, ended_at = ? WHERE id = ?",
            (now - 1, now - kb.RECLAIM_DEFER_GRACE_SECONDS - 1, run_id),
        )
    assert kb.reconcile_terminal_worker_cleanups(conn) == []
    assert kb.claim_task(conn, second) is None

    monkeypatch.setattr(kb, "_worker_identity_matches", lambda *_a, **_kw: False)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    assert kb.reconcile_terminal_worker_cleanups(conn) == [first]
    assert kb.claim_task(conn, second) is not None


def test_unregistered_completed_worker_lease_has_bounded_auditable_fallback(
    conn, tmp_path,
):
    workspace = tmp_path / "unregistered-lease"
    first = kb.create_task(
        conn, title="first", workspace_kind="dir", workspace_path=str(workspace),
    )
    second = kb.create_task(
        conn, title="second", workspace_kind="dir", workspace_path=str(workspace),
    )
    claimed = kb.claim_task(conn, first)
    assert claimed is not None
    run_id = claimed.current_run_id
    assert run_id is not None

    assert kb.complete_task(conn, first, expected_run_id=run_id)
    assert kb.reconcile_terminal_worker_cleanups(conn) == []
    assert kb.claim_task(conn, second) is None

    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET claim_expires = ?, ended_at = ? WHERE id = ?",
            (now - 1, now - kb.RECLAIM_DEFER_GRACE_SECONDS - 1, run_id),
        )
    assert kb.reconcile_terminal_worker_cleanups(conn) == [first]
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'worker_cleanup_verified' ORDER BY id DESC LIMIT 1",
        (first,),
    ).fetchone()
    assert event is not None
    assert json.loads(event["payload"])["reason"] == (
        "unregistered_worker_lease_expired"
    )
    assert kb.claim_task(conn, second) is not None


def test_registered_pid_reuse_lease_expires_without_signalling_new_generation(
    conn, tmp_path, monkeypatch,
):
    workspace = tmp_path / "registered-reuse-lease"
    first = kb.create_task(
        conn, title="first", workspace_kind="dir", workspace_path=str(workspace),
    )
    second = kb.create_task(
        conn, title="second", workspace_kind="dir", workspace_path=str(workspace),
    )
    claimed = kb.claim_task(conn, first)
    assert claimed is not None
    run_id = claimed.current_run_id
    assert run_id is not None
    reused_pid = 987654
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (reused_pid, first),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, worker_pgid = ?, "
            "worker_sid = ?, worker_start_time = ? WHERE id = ?",
            (reused_pid, reused_pid, reused_pid, "old-generation", run_id),
        )

    signals = []
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == reused_pid)
    monkeypatch.setattr(kb, "_worker_identity_matches", lambda *_a, **_kw: False)
    monkeypatch.setattr(kb.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    assert kb.complete_task(conn, first, expected_run_id=run_id)
    assert kb.reconcile_terminal_worker_cleanups(conn) == []
    assert kb.claim_task(conn, second) is None

    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET claim_expires = ?, ended_at = ? WHERE id = ?",
            (now - 1, now - kb.RECLAIM_DEFER_GRACE_SECONDS - 1, run_id),
        )
    assert kb.reconcile_terminal_worker_cleanups(conn) == [first]
    assert signals == []
    event = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? "
        "AND kind = 'worker_cleanup_verified' ORDER BY id DESC LIMIT 1",
        (first,),
    ).fetchone()
    assert event is not None
    assert json.loads(event["payload"])["reason"] == (
        "unverified_worker_lease_expired"
    )
    assert kb.claim_task(conn, second) is not None


def test_spawn_registration_losing_completion_cas_reaps_worker_and_releases_lease(
    conn, tmp_path,
):
    workspace = tmp_path / "spawn-race"
    first = kb.create_task(
        conn, title="first", workspace_kind="dir", workspace_path=str(workspace),
    )
    second = kb.create_task(
        conn, title="second", workspace_kind="dir", workspace_path=str(workspace),
    )
    claimed = kb.claim_task(conn, first)
    assert claimed is not None
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert kb.complete_task(
            conn, first, expected_run_id=claimed.current_run_id,
        )
        assert kb.claim_task(conn, second) is None

        assert kb._set_worker_pid(
            conn,
            first,
            worker.pid,
            expected_run_id=claimed.current_run_id,
            expected_claim_lock=claimed.claim_lock,
        ) is False
        worker.wait(timeout=5)
        task = kb.get_task(conn, first)
        assert task is not None
        assert task.worker_pid is None
        cleanup_verified = conn.execute(
            "SELECT cleanup_verified FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()[0]
        assert cleanup_verified == 1
        assert kb.claim_task(conn, second) is not None
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
