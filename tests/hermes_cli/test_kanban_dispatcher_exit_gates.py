"""Regression tests for dispatcher-supervised worker exit serialization.

These tests exercise behavior, not implementation shape: once the dispatcher
has captured a worker's node, boot, PID-start token, and isolated process
group, no reclaim path may make the task claimable again until that exact
process group is proven gone.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


_NODE_ID = "dispatcher-test-node"
_BOOT_ID = "dispatcher-test-boot"
_START_TOKEN = "100"
_WORKER_PID = 424_242


_BARRIER_CHILD_CODE = r"""
import os
import sqlite3
import sys
from pathlib import Path

# Importing the real entrypoint crosses the production early-start barrier
# before config, plugins, or task-work code can run.
import hermes_cli.main  # noqa: F401

conn = sqlite3.connect(os.environ["BARRIER_TEST_DB"])
task_id = os.environ["BARRIER_TEST_TASK_ID"]
expected_run_id = int(os.environ["BARRIER_TEST_RUN_ID"])
expected_claim_lock = os.environ["BARRIER_TEST_CLAIM_LOCK"]
task = conn.execute(
    "SELECT status, current_run_id, claim_lock, worker_pid FROM tasks WHERE id = ?",
    (task_id,),
).fetchone()
run = conn.execute(
    "SELECT status, ended_at, claim_lock, worker_pid FROM task_runs WHERE id = ?",
    (expected_run_id,),
).fetchone()
conn.close()
if task != ("running", expected_run_id, expected_claim_lock, os.getpid()):
    sys.exit(76)
if run != ("running", None, expected_claim_lock, os.getpid()):
    sys.exit(77)
Path(os.environ["BARRIER_TEST_WORK_MARKER"]).write_text(
    f"{task_id}:{expected_run_id}:{os.getpid()}",
    encoding="utf-8",
)
"""


@pytest.fixture
def supervised_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(board="default")
    return home, project


def _identity(pid: int = _WORKER_PID, *, start: str = _START_TOKEN) -> dict:
    return {
        "owner_node_id": _NODE_ID,
        "owner_boot_id": _BOOT_ID,
        "worker_pid": int(pid),
        "worker_start_token": str(start),
        "worker_pgid": int(pid),
    }


def _install_supervised_run(
    conn,
    project: Path,
    *,
    pid: int = _WORKER_PID,
    max_runtime_seconds: int | None = None,
    max_retries: int | None = None,
) -> dict:
    task_id = kb.create_task(
        conn,
        title="supervised worker",
        assignee="worker",
        workspace_kind="dir",
        workspace_path=str(project),
        max_runtime_seconds=max_runtime_seconds,
        max_retries=max_retries,
    )
    host = kb._claimer_id().split(":", 1)[0]
    claimed = kb.claim_task(conn, task_id, claimer=f"{host}:strict-exit-test")
    assert claimed is not None
    run_id = int(claimed.current_run_id)
    claim_lock = str(claimed.claim_lock)
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET worker_pid = ?, started_at = ?, "
            "last_heartbeat_at = NULL WHERE id = ?",
            (pid, now, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ?, owner_node_id = ?, "
            "owner_boot_id = ?, worker_start_token = ?, worker_pgid = ?, "
            "handoff_safety_required = 1, started_at = ?, "
            "last_heartbeat_at = NULL WHERE id = ?",
            (
                pid,
                _NODE_ID,
                _BOOT_ID,
                _START_TOKEN,
                pid,
                now,
                run_id,
            ),
        )
    return {
        "task_id": task_id,
        "run_id": run_id,
        "claim_lock": claim_lock,
        "pid": pid,
    }


def _patch_same_live_worker(monkeypatch) -> None:
    monkeypatch.setattr(kb, "_local_node_id", lambda: _NODE_ID)
    monkeypatch.setattr(kb, "_local_boot_id", lambda: _BOOT_ID)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: True)
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda pid: _identity(int(pid)),
    )
    monkeypatch.setattr(
        "gateway.status.get_process_start_time",
        lambda _pid: int(_START_TOKEN),
    )


def _open_gate(conn, task_id: str):
    return conn.execute(
        "SELECT * FROM task_exit_gates WHERE child_task_id = ? "
        "AND released_at IS NULL",
        (task_id,),
    ).fetchone()


def _claim_barrier_task(conn, project: Path) -> dict:
    task_id = kb.create_task(
        conn,
        title="real start-barrier worker",
        assignee="worker",
        workspace_kind="dir",
        workspace_path=str(project),
    )
    claimed = kb.claim_task(conn, task_id, claimer="start-barrier-dispatcher")
    assert claimed is not None
    return {
        "task_id": task_id,
        "run_id": int(claimed.current_run_id),
        "claim_lock": str(claimed.claim_lock),
    }


def _spawn_real_barrier_child(
    *,
    db_path: Path,
    marker: Path,
    task_id: str,
    run_id: int,
    claim_lock: str,
) -> tuple[subprocess.Popen, int]:
    read_fd, release_fd = os.pipe()
    os.set_inheritable(read_fd, True)
    env = dict(os.environ)
    env[kb._KANBAN_START_BARRIER_ENV] = str(read_fd)
    env["BARRIER_TEST_DB"] = str(db_path)
    env["BARRIER_TEST_WORK_MARKER"] = str(marker)
    env["BARRIER_TEST_TASK_ID"] = task_id
    env["BARRIER_TEST_RUN_ID"] = str(run_id)
    env["BARRIER_TEST_CLAIM_LOCK"] = claim_lock
    source_root = Path(__file__).resolve().parents[2]
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _BARRIER_CHILD_CODE],
            cwd=str(source_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            pass_fds=(read_fd,),
        )
    except Exception:
        os.close(read_fd)
        os.close(release_fd)
        raise
    os.close(read_fd)
    kb._register_pending_worker_start(proc, release_fd, "default")

    deadline = time.monotonic() + 5.0
    identity = None
    while time.monotonic() < deadline:
        identity = kb._capture_process_group_identity(proc.pid)
        if identity is not None:
            break
        if proc.poll() is not None:
            break
        time.sleep(0.01)
    if identity is None:
        pending = kb._take_pending_worker_start(proc.pid)
        if pending is not None:
            kb._abort_pending_worker_start(pending)
        stderr = proc.stderr.read() if proc.poll() is not None else ""
        pytest.fail(
            "real barrier child did not expose a durable isolated identity: "
            f"rc={proc.poll()} stderr={stderr}"
        )
    assert proc.poll() is None
    return proc, release_fd


def _abort_test_child(proc: subprocess.Popen) -> None:
    pending = kb._take_pending_worker_start(proc.pid)
    if pending is not None:
        kb._abort_pending_worker_start(pending)
    elif proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2.0)


def _assert_failed_barrier_child_cleaned(
    proc: subprocess.Popen,
    release_fd: int,
    marker: Path,
) -> None:
    assert proc.returncode == 75
    assert marker.exists() is False
    with kb._PENDING_WORKER_STARTS_LOCK:
        assert proc.pid not in kb._PENDING_WORKER_STARTS
    with pytest.raises(OSError):
        os.fstat(release_fd)
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


@pytest.mark.parametrize("path", ["manual", "ttl", "stale", "timeout"])
def test_supervised_forced_exit_paths_persist_gate_before_signal(
    supervised_home, monkeypatch, path
):
    """Every forced-exit entrypoint must durably gate before signalling.

    A permission failure is the strongest ordering probe: the signal callback
    inspects the database before raising. If it can see the gate, a dispatcher
    crash or EPERM at that boundary cannot expose a replacement worker.
    """
    _, project = supervised_home
    _patch_same_live_worker(monkeypatch)

    with kb.connect() as conn:
        run = _install_supervised_run(
            conn,
            project,
            max_runtime_seconds=1 if path == "timeout" else None,
        )
        task_id = run["task_id"]
        old = int(time.time()) - (5 * 3600)
        with kb.write_txn(conn):
            if path == "ttl":
                conn.execute(
                    "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
                    "WHERE id = ?",
                    (old, old, task_id),
                )
            elif path in {"stale", "timeout"}:
                conn.execute(
                    "UPDATE tasks SET started_at = ?, last_heartbeat_at = NULL "
                    "WHERE id = ?",
                    (old, task_id),
                )
                conn.execute(
                    "UPDATE task_runs SET started_at = ?, last_heartbeat_at = NULL "
                    "WHERE id = ?",
                    (old, run["run_id"]),
                )

        signal_calls: list[tuple[int, int]] = []

        def _permission_denied(pgid: int, sig: int) -> None:
            signal_calls.append((pgid, sig))
            gate = _open_gate(conn, task_id)
            assert gate is not None, "exit gate must commit before signalling"
            assert gate["parent_run_id"] == run["run_id"]
            assert kb.get_task(conn, task_id).status == "todo"
            raise PermissionError("test denies process-group signal")

        if path == "manual":
            result = kb.reclaim_task(
                conn,
                task_id,
                reason="operator abort",
                signal_fn=_permission_denied,
            )
            assert result is True
        elif path == "ttl":
            assert kb.release_stale_claims(
                conn, signal_fn=_permission_denied
            ) == 1
        elif path == "stale":
            assert kb.detect_stale_running(
                conn,
                stale_timeout_seconds=4 * 3600,
                signal_fn=_permission_denied,
            ) == [task_id]
        else:
            assert kb.enforce_max_runtime(
                conn, signal_fn=_permission_denied
            ) == [task_id]

        assert signal_calls == [(_WORKER_PID, signal.SIGTERM)]
        gate = _open_gate(conn, task_id)
        assert gate is not None
        assert gate["gate_kind"] == "control_drain"
        assert gate["child_task_id"] == task_id
        assert gate["parent_task_id"] == task_id
        assert gate["parent_run_id"] == run["run_id"]
        assert gate["worker_pid"] == _WORKER_PID
        assert gate["worker_start_token"] == _START_TOKEN
        assert gate["worker_pgid"] == _WORKER_PID

        task = kb.get_task(conn, task_id)
        assert task.status == "todo"
        assert task.worker_pid == _WORKER_PID
        assert task.claim_lock == run["claim_lock"]
        assert task.current_run_id is None
        assert kb.claim_task(conn, task_id) is None

        stored_run = conn.execute(
            "SELECT * FROM task_runs WHERE id = ?", (run["run_id"],)
        ).fetchone()
        assert stored_run["ended_at"] is not None
        assert stored_run["worker_pid"] == _WORKER_PID
        assert stored_run["claim_lock"] == run["claim_lock"]
        assert any(
            event.kind == "process_exit_signal_failed"
            for event in kb.list_events(conn, task_id)
        )


def test_pid_start_token_mismatch_is_never_signalled_or_replaced(
    supervised_home, monkeypatch
):
    """A reused PID is not the old worker and must never receive a signal."""
    _, project = supervised_home
    monkeypatch.setattr(kb, "_local_node_id", lambda: _NODE_ID)
    monkeypatch.setattr(kb, "_local_boot_id", lambda: _BOOT_ID)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: True)
    monkeypatch.setattr(
        kb,
        "_capture_process_group_identity",
        lambda pid: _identity(int(pid), start="101"),
    )
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: 101)

    with kb.connect() as conn:
        run = _install_supervised_run(conn, project, max_runtime_seconds=1)
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, run["run_id"]),
            )

        signals: list[tuple[int, int]] = []
        assert kb.enforce_max_runtime(
            conn,
            signal_fn=lambda pgid, sig: signals.append((pgid, sig)),
        ) == [run["task_id"]]

        assert signals == []
        assert _open_gate(conn, run["task_id"]) is not None
        assert kb.claim_task(conn, run["task_id"]) is None
        assert kb.release_handoff_exit_gates(conn) == 0
        assert _open_gate(conn, run["task_id"]) is not None
        assert any(
            event.kind == "process_exit_signal_failed"
            and "identity changed" in (event.payload or {}).get("error", "")
            for event in kb.list_events(conn, run["task_id"])
        )


@pytest.mark.parametrize(
    ("max_retries", "expected_status"),
    [(None, "todo"), (1, "blocked")],
)
def test_managed_runtime_timeout_accounts_failure_in_same_gate_commit(
    supervised_home, monkeypatch, max_retries, expected_status
):
    _, project = supervised_home
    _patch_same_live_worker(monkeypatch)
    with kb.connect() as conn:
        run = _install_supervised_run(
            conn,
            project,
            max_runtime_seconds=1,
            max_retries=max_retries,
        )
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, run["run_id"]),
            )

        def signal_denied(_pgid, _sig):
            raise PermissionError("keep gate open for assertions")

        assert kb.enforce_max_runtime(
            conn, signal_fn=signal_denied
        ) == [run["task_id"]]

        task = kb.get_task(conn, run["task_id"])
        stored_run = kb.latest_run(conn, run["task_id"])
        kinds = [event.kind for event in kb.list_events(conn, run["task_id"])]
        assert task.status == expected_status
        assert task.consecutive_failures == 1
        assert task.worker_pid == _WORKER_PID
        assert stored_run.status == "timed_out"
        assert stored_run.worker_pid == _WORKER_PID
        assert _open_gate(conn, run["task_id"]) is not None
        assert kinds.count("timed_out") == 1
        assert kinds.count("gave_up") == (1 if expected_status == "blocked" else 0)


def test_managed_runtime_timeout_rolls_back_failure_and_gate_together(
    supervised_home, monkeypatch
):
    _, project = supervised_home
    _patch_same_live_worker(monkeypatch)
    with kb.connect() as conn:
        run = _install_supervised_run(
            conn,
            project,
            max_runtime_seconds=1,
            max_retries=1,
        )
        old = int(time.time()) - 30
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET started_at = ? WHERE id = ?",
                (old, run["run_id"]),
            )
        real_append_event = kb._append_event

        def fail_gave_up(*args, **kwargs):
            kind = args[2] if len(args) > 2 else kwargs.get("kind")
            if kind == "gave_up":
                raise RuntimeError("injected atomic timeout failure")
            return real_append_event(*args, **kwargs)

        monkeypatch.setattr(kb, "_append_event", fail_gave_up)
        with pytest.raises(RuntimeError, match="injected atomic timeout failure"):
            kb.enforce_max_runtime(conn, signal_fn=lambda *_args: None)
        monkeypatch.setattr(kb, "_append_event", real_append_event)

        task = kb.get_task(conn, run["task_id"])
        stored_run = kb.latest_run(conn, run["task_id"])
        assert task.status == "running"
        assert task.current_run_id == run["run_id"]
        assert task.consecutive_failures == 0
        assert stored_run.status == "running"
        assert stored_run.ended_at is None
        assert _open_gate(conn, run["task_id"]) is None


def test_crashed_leader_with_live_descendant_never_becomes_claimable(
    supervised_home, monkeypatch
):
    """Leader death is insufficient while its captured PGID remains alive."""
    _, project = supervised_home
    monkeypatch.setattr(kb, "_local_node_id", lambda: _NODE_ID)
    monkeypatch.setattr(kb, "_local_boot_id", lambda: _BOOT_ID)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: True)
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: None)

    with kb.connect() as conn:
        run = _install_supervised_run(conn, project)
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET started_at = started_at - 60 WHERE id = ?",
                (run["task_id"],),
            )
            conn.execute(
                "UPDATE task_runs SET started_at = started_at - 60 WHERE id = ?",
                (run["run_id"],),
            )

        assert kb.detect_crashed_workers(conn) == []
        task = kb.get_task(conn, run["task_id"])
        assert task.status == "running"
        assert task.current_run_id == run["run_id"]
        assert task.worker_pid == _WORKER_PID
        assert kb.claim_task(conn, run["task_id"]) is None

        spawned: list[str] = []
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda task, _workspace: spawned.append(task.id),
        )
        assert result.spawned == []
        assert spawned == []
        assert kb.get_task(conn, run["task_id"]).status == "running"


def test_exit_gate_release_allows_exactly_one_new_claim(
    supervised_home, monkeypatch
):
    """A replacement becomes possible only after full group-exit proof."""
    _, project = supervised_home
    _patch_same_live_worker(monkeypatch)

    with kb.connect() as conn:
        run = _install_supervised_run(conn, project)

        def _permission_denied(_pgid: int, _sig: int) -> None:
            raise PermissionError("still draining")

        assert kb.reclaim_task(
            conn,
            run["task_id"],
            reason="replace safely",
            signal_fn=_permission_denied,
        ) is True
        assert _open_gate(conn, run["task_id"]) is not None
        assert kb.claim_task(conn, run["task_id"]) is None

        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kb, "_process_group_alive", lambda _pgid: False)
        monkeypatch.setattr("gateway.status.get_process_start_time", lambda _pid: None)

        assert kb.release_handoff_exit_gates(conn) == 1
        gate = conn.execute(
            "SELECT * FROM task_exit_gates WHERE child_task_id = ?",
            (run["task_id"],),
        ).fetchone()
        assert gate["released_at"] is not None
        assert gate["release_reason"] == "process_group_exited"

        task = kb.get_task(conn, run["task_id"])
        assert task.status == "ready"
        assert task.worker_pid is None
        assert task.claim_lock is None
        first = kb.claim_task(conn, run["task_id"], claimer="replacement")
        assert first is not None
        assert kb.claim_task(conn, run["task_id"], claimer="duplicate") is None


def test_stale_snapshot_cannot_gate_or_signal_a_new_run(
    supervised_home, monkeypatch
):
    """A reclaim snapshot from run A cannot mutate or signal run B."""
    _, project = supervised_home
    _patch_same_live_worker(monkeypatch)

    with kb.connect() as conn:
        old = _install_supervised_run(conn, project)
        now = int(time.time())
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE task_runs SET status = 'reclaimed', outcome = 'reclaimed', "
                "ended_at = ?, claim_lock = NULL, claim_expires = NULL, "
                "worker_pid = NULL WHERE id = ?",
                (now, old["run_id"]),
            )
            conn.execute(
                "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                "WHERE id = ?",
                (old["task_id"],),
            )

        replacement = kb.claim_task(
            conn, old["task_id"], claimer="replacement-owner"
        )
        assert replacement is not None
        replacement_run_id = int(replacement.current_run_id)
        replacement_pid = _WORKER_PID + 1
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET worker_pid = ? WHERE id = ?",
                (replacement_pid, old["task_id"]),
            )
            conn.execute(
                "UPDATE task_runs SET worker_pid = ?, owner_node_id = ?, "
                "owner_boot_id = ?, worker_start_token = ?, worker_pgid = ?, "
                "handoff_safety_required = 1 "
                "WHERE id = ?",
                (
                    replacement_pid,
                    _NODE_ID,
                    _BOOT_ID,
                    "200",
                    replacement_pid,
                    replacement_run_id,
                ),
            )

        parked = kb._park_supervised_worker_for_exit(
            conn,
            old["task_id"],
            outcome="reclaimed",
            event_kind="reclaimed",
            error="stale snapshot",
            expected_run_id=old["run_id"],
            expected_worker_pid=old["pid"],
            expected_claim_lock=old["claim_lock"],
        )
        assert parked is None
        assert conn.execute(
            "SELECT COUNT(*) FROM task_exit_gates WHERE child_task_id = ?",
            (old["task_id"],),
        ).fetchone()[0] == 0

        task = kb.get_task(conn, old["task_id"])
        assert task.status == "running"
        assert task.current_run_id == replacement_run_id
        assert task.worker_pid == replacement_pid
        assert task.claim_lock == "replacement-owner"


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 start barrier is POSIX-only")
def test_real_child_cannot_do_task_work_before_registration_commit_and_release(
    supervised_home, tmp_path
):
    """The actual early-entrypoint barrier precedes every task side effect."""
    _, project = supervised_home
    marker = tmp_path / "happy-child-task-work.started"
    proc = None
    with kb.connect() as conn:
        run = _claim_barrier_task(conn, project)
        proc, release_fd = _spawn_real_barrier_child(
            db_path=kb.kanban_db_path(board="default"),
            marker=marker,
            task_id=run["task_id"],
            run_id=run["run_id"],
            claim_lock=run["claim_lock"],
        )
        try:
            # Give the child enough time to reach the barrier.  It must remain
            # alive, perform no task work, and remain absent from durable task
            # ownership until the registration transaction commits.
            time.sleep(0.15)
            assert proc.poll() is None
            assert marker.exists() is False
            task_before = kb.get_task(conn, run["task_id"])
            run_before = conn.execute(
                "SELECT worker_pid FROM task_runs WHERE id = ?",
                (run["run_id"],),
            ).fetchone()
            assert task_before.worker_pid is None
            assert run_before["worker_pid"] is None

            assert kb._set_worker_pid(
                conn,
                run["task_id"],
                proc.pid,
                expected_run_id=run["run_id"],
                expected_claim_lock=run["claim_lock"],
            ) is True

            stdout, stderr = proc.communicate(timeout=15.0)
            assert proc.returncode == 0, f"stdout={stdout!r} stderr={stderr!r}"
            assert marker.read_text(encoding="utf-8") == (
                f"{run['task_id']}:{run['run_id']}:{proc.pid}"
            )
            task_after = kb.get_task(conn, run["task_id"])
            run_after = conn.execute(
                "SELECT worker_pid, owner_node_id, owner_boot_id, "
                "worker_start_token, worker_pgid FROM task_runs WHERE id = ?",
                (run["run_id"],),
            ).fetchone()
            assert task_after.worker_pid == proc.pid
            assert run_after["worker_pid"] == proc.pid
            assert run_after["owner_node_id"]
            assert run_after["owner_boot_id"]
            assert run_after["worker_start_token"]
            assert run_after["worker_pgid"] == proc.pid
            with kb._PENDING_WORKER_STARTS_LOCK:
                assert proc.pid not in kb._PENDING_WORKER_STARTS
            with pytest.raises(OSError):
                os.fstat(release_fd)
        finally:
            _abort_test_child(proc)


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 start barrier is POSIX-only")
def test_real_child_stale_registration_cas_exits_75_and_preserves_new_run(
    supervised_home, tmp_path
):
    """An old launch cannot work on, register against, or erase a new run."""
    _, project = supervised_home
    marker = tmp_path / "stale-child-task-work.started"
    proc = None
    with kb.connect() as conn:
        old = _claim_barrier_task(conn, project)
        proc, release_fd = _spawn_real_barrier_child(
            db_path=kb.kanban_db_path(board="default"),
            marker=marker,
            task_id=old["task_id"],
            run_id=old["run_id"],
            claim_lock=old["claim_lock"],
        )
        try:
            now = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET status = 'reclaimed', "
                    "outcome = 'reclaimed', ended_at = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                    (now, old["run_id"]),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                    "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                    "WHERE id = ?",
                    (old["task_id"],),
                )
            newer = kb.claim_task(
                conn, old["task_id"], claimer="newer-start-barrier-owner"
            )
            assert newer is not None
            newer_run_id = int(newer.current_run_id)
            task_before = dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (old["task_id"],),
                ).fetchone()
            )
            run_before = dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (newer_run_id,),
                ).fetchone()
            )

            assert kb._set_worker_pid(
                conn,
                old["task_id"],
                proc.pid,
                expected_run_id=old["run_id"],
                expected_claim_lock=old["claim_lock"],
            ) is False
            _assert_failed_barrier_child_cleaned(proc, release_fd, marker)

            task_after = dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (old["task_id"],),
                ).fetchone()
            )
            run_after = dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (newer_run_id,),
                ).fetchone()
            )
            assert task_after == task_before
            assert run_after == run_before
        finally:
            _abort_test_child(proc)


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 start barrier is POSIX-only")
def test_real_child_identity_unavailable_exits_75_without_task_work(
    supervised_home, tmp_path, monkeypatch
):
    """Missing durable identity fails closed before DB registration or work."""
    _, project = supervised_home
    marker = tmp_path / "identity-child-task-work.started"
    proc = None
    with kb.connect() as conn:
        run = _claim_barrier_task(conn, project)
        proc, release_fd = _spawn_real_barrier_child(
            db_path=kb.kanban_db_path(board="default"),
            marker=marker,
            task_id=run["task_id"],
            run_id=run["run_id"],
            claim_lock=run["claim_lock"],
        )
        try:
            task_before = dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (run["task_id"],),
                ).fetchone()
            )
            run_before = dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (run["run_id"],),
                ).fetchone()
            )
            monkeypatch.setattr(kb, "_capture_process_group_identity", lambda _pid: None)

            with pytest.raises(
                RuntimeError,
                match="durable worker identity was unavailable",
            ):
                kb._set_worker_pid(
                    conn,
                    run["task_id"],
                    proc.pid,
                    expected_run_id=run["run_id"],
                    expected_claim_lock=run["claim_lock"],
                )
            _assert_failed_barrier_child_cleaned(proc, release_fd, marker)
            assert dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (run["task_id"],),
                ).fetchone()
            ) == task_before
            assert dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (run["run_id"],),
                ).fetchone()
            ) == run_before
        finally:
            _abort_test_child(proc)


@pytest.mark.skipif(os.name == "nt", reason="Phase 1 start barrier is POSIX-only")
def test_real_child_release_write_failure_exits_75_and_preserves_new_run(
    supervised_home, tmp_path, monkeypatch
):
    """A failed release reaps the exact child without rolling back newer state."""
    _, project = supervised_home
    marker = tmp_path / "release-child-task-work.started"
    proc = None
    with kb.connect() as conn:
        old = _claim_barrier_task(conn, project)
        proc, release_fd = _spawn_real_barrier_child(
            db_path=kb.kanban_db_path(board="default"),
            marker=marker,
            task_id=old["task_id"],
            run_id=old["run_id"],
            claim_lock=old["claim_lock"],
        )
        real_write = os.write
        newer_state: dict[str, object] = {}

        def _fail_release_after_new_run(fd: int, data: bytes) -> int:
            if fd != release_fd:
                return real_write(fd, data)
            now = int(time.time())
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE task_runs SET status = 'reclaimed', "
                    "outcome = 'reclaimed', ended_at = ?, claim_lock = NULL, "
                    "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                    (now, old["run_id"]),
                )
                conn.execute(
                    "UPDATE tasks SET status = 'ready', current_run_id = NULL, "
                    "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                    "WHERE id = ?",
                    (old["task_id"],),
                )
            newer = kb.claim_task(
                conn, old["task_id"], claimer="release-race-newer-owner"
            )
            assert newer is not None
            newer_state["run_id"] = int(newer.current_run_id)
            newer_state["task"] = dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (old["task_id"],),
                ).fetchone()
            )
            newer_state["run"] = dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (int(newer.current_run_id),),
                ).fetchone()
            )
            raise OSError("simulated start-barrier release failure")

        monkeypatch.setattr(kb.os, "write", _fail_release_after_new_run)
        try:
            with pytest.raises(
                RuntimeError,
                match="registered worker could not cross its start barrier",
            ):
                kb._set_worker_pid(
                    conn,
                    old["task_id"],
                    proc.pid,
                    expected_run_id=old["run_id"],
                    expected_claim_lock=old["claim_lock"],
                )
            _assert_failed_barrier_child_cleaned(proc, release_fd, marker)
            assert newer_state
            assert dict(
                conn.execute(
                    "SELECT status, current_run_id, claim_lock, worker_pid "
                    "FROM tasks WHERE id = ?",
                    (old["task_id"],),
                ).fetchone()
            ) == newer_state["task"]
            assert dict(
                conn.execute(
                    "SELECT status, ended_at, claim_lock, worker_pid "
                    "FROM task_runs WHERE id = ?",
                    (newer_state["run_id"],),
                ).fetchone()
            ) == newer_state["run"]
        finally:
            _abort_test_child(proc)
