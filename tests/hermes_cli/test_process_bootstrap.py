from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.attempt_fence_helpers import isolated_home


pytestmark = pytest.mark.skipif(
    os.uname().sysname != "Darwin",
    reason="attempt-fence worker bootstrap is macOS-only",
)


def test_bootstrap_cannot_execute_before_db_bind(tmp_path):
    marker = tmp_path / "executed"
    pending = kb._spawn_behind_bootstrap(
        ["/usr/bin/touch", str(marker)],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )
    time.sleep(0.1)
    assert not marker.exists()
    pending.release()
    assert pending.proc.wait(timeout=5) == 0
    assert marker.exists()


def test_bootstrap_parent_death_yields_eof_without_exec(tmp_path):
    marker = tmp_path / "must-not-execute"
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [
            os.sys.executable,
            "-m",
            "hermes_cli.process_bootstrap",
            "--gate-fd",
            str(read_fd),
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    os.close(write_fd)

    assert proc.wait(timeout=5) == 125
    assert not marker.exists()


def test_separate_dispatcher_process_death_closes_gate_before_exec(tmp_path):
    marker = tmp_path / "must-not-execute-after-parent-death"
    child_pid_path = tmp_path / "bootstrap-pid"
    script = (
        "import os,pathlib,subprocess; "
        "from hermes_cli import kanban_db as kb; "
        "p=kb._spawn_behind_bootstrap("
        f"['/usr/bin/touch',{str(marker)!r}],env=os.environ,"
        f"cwd={str(tmp_path)!r},stdout=subprocess.DEVNULL); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.proc.pid)); "
        "os._exit(0)"
    )
    parent = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=os.environ,
        check=False,
        timeout=5,
    )
    assert parent.returncode == 0
    bootstrap_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 5
    while kb._darwin_process_identity(bootstrap_pid) is not None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    assert not marker.exists()


def test_bootstrap_without_command_exits_usage_error(tmp_path):
    proc = subprocess.run(
        [
            os.sys.executable,
            "-m",
            "hermes_cli.process_bootstrap",
            "--gate-fd",
            "0",
            "--",
        ],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    assert proc.returncode == 64


def test_bootstrap_rejects_non_release_token_without_exec(tmp_path):
    marker = tmp_path / "must-not-execute-invalid-token"
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [
            os.sys.executable,
            "-m",
            "hermes_cli.process_bootstrap",
            "--gate-fd",
            str(read_fd),
            "--",
            "/usr/bin/touch",
            str(marker),
        ],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=(read_fd,),
        start_new_session=True,
    )
    os.close(read_fd)
    os.write(write_fd, b"0")
    os.close(write_fd)
    assert proc.wait(timeout=5) == 125
    assert not marker.exists()


def test_real_popen_failure_leaks_no_fd_and_records_no_process_fence(
    isolated_home,
    tmp_path,
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="popen failure", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        assert claimed.current_run_id is not None
        assert claimed.claim_lock is not None
        before = set(os.listdir("/dev/fd"))
        with pytest.raises(kb.SpawnStartError):
            kb._spawn_behind_bootstrap(
                ["/usr/bin/true"],
                env=os.environ,
                cwd=str(tmp_path),
                stdout=subprocess.DEVNULL,
                python_executable="/definitely/missing/python",
            )
        assert set(os.listdir("/dev/fd")) == before

        kb._handle_spawn_start_failure(
            conn,
            task_id,
            error="fixture popen failure",
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        task = kb.get_task(conn, task_id)
        assert task.worker_fence is None
        assert task.worker_pid is None and task.worker_pgid is None
        assert task.status in {"ready", "blocked"}
    finally:
        conn.close()


def test_release_error_closes_fd_and_aborts_verified_group(tmp_path, monkeypatch):
    before = set(os.listdir("/dev/fd"))
    pending = kb._spawn_behind_bootstrap(
        ["/bin/sleep", "60"],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )

    def fail_write(*_args):
        raise OSError("fd")

    monkeypatch.setattr(os, "write", fail_write)
    with pytest.raises(OSError, match="fd"):
        pending.release()
    assert pending.gate_write_fd == -1
    assert pending.proc.poll() is not None
    assert set(os.listdir("/dev/fd")) == before


def test_abort_is_idempotent_waits_and_leaks_no_fd(tmp_path):
    before = set(os.listdir("/dev/fd"))
    pending = kb._spawn_behind_bootstrap(
        ["/bin/sleep", "60"],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )
    pending.abort()
    pending.abort()
    assert pending.proc.poll() is not None
    assert pending.gate_write_fd == -1
    assert set(os.listdir("/dev/fd")) == before


def test_abort_terminates_verified_group_after_gate_token(tmp_path):
    pending = kb._spawn_behind_bootstrap(
        ["/bin/sleep", "60"],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )
    os.write(pending.gate_write_fd, b"1")
    time.sleep(0.1)
    pending.abort()
    assert pending.proc.poll() is not None
    assert pending.proc.returncode != 0


def test_abort_escalates_until_entire_verified_group_is_dead(tmp_path):
    child_pid_path = tmp_path / "child-pid"
    command = (
        "import pathlib,signal,subprocess,time; "
        "p=subprocess.Popen(["
        "sys.executable,'-c','import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid)); "
        "time.sleep(60)"
    )
    pending = kb._spawn_behind_bootstrap(
        [sys.executable, "-c", "import sys; " + command],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )
    try:
        pending.release()
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        pending.abort()
        assert pending._group_has_live_members() is False
    finally:
        try:
            os.killpg(pending.identity.pgid, 9)
        except ProcessLookupError:
            pass
        pending.proc.wait(timeout=5)


def test_abort_never_signals_reused_group_after_reaped_leader(
    tmp_path,
    monkeypatch,
):
    pending = kb._spawn_behind_bootstrap(
        ["/usr/bin/true"],
        env=os.environ,
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
    )
    pending.release()
    assert pending.proc.wait(timeout=5) == 0
    signals = []
    monkeypatch.setattr(kb, "_identity_matches", lambda _identity: False)
    monkeypatch.setattr(
        pending,
        "_group_has_live_members",
        lambda: True,
    )
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))

    with pytest.raises(kb.UnknownWorkerProcess, match="reused process identity"):
        pending.abort()
    assert signals == []


def test_spawn_start_failure_lost_claim_is_zero_delta(isolated_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="lost start", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        assert claimed.current_run_id is not None
        assert claimed.claim_lock is not None
        conn.execute(
            "UPDATE tasks SET claim_lock='fixture:new' WHERE id=?",
            (task_id,),
        )
        conn.commit()
        def snapshot():
            return (
                tuple(tuple(row) for row in conn.execute("SELECT * FROM tasks")),
                tuple(tuple(row) for row in conn.execute("SELECT * FROM task_runs")),
                tuple(tuple(row) for row in conn.execute("SELECT * FROM task_events")),
            )

        before = snapshot()
        assert not kb._handle_spawn_start_failure(
            conn,
            task_id,
            error="late failure",
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        assert snapshot() == before
    finally:
        conn.close()


def test_spawn_start_failure_lost_run_claim_is_zero_delta(isolated_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="lost run start", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        conn.execute(
            "UPDATE task_runs SET claim_lock='fixture:new' WHERE id=?",
            (claimed.current_run_id,),
        )
        conn.commit()

        def snapshot():
            return (
                tuple(tuple(row) for row in conn.execute("SELECT * FROM tasks")),
                tuple(tuple(row) for row in conn.execute("SELECT * FROM task_runs")),
                tuple(tuple(row) for row in conn.execute("SELECT * FROM task_events")),
            )

        before = snapshot()
        assert not kb._handle_spawn_start_failure(
            conn,
            task_id,
            error="late run failure",
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        assert snapshot() == before
    finally:
        conn.close()


def test_bind_pending_worker_atomically_copies_exact_fence(isolated_home, tmp_path):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="bind", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        assert kb._bind_pending_worker(
            conn,
            task_id,
            pending,
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        assert task.worker_pid == pending.identity.pid
        assert task.worker_pgid == pending.identity.pgid
        assert task.worker_identity == pending.identity.token
        assert task.worker_fence == run.worker_fence
        fence = json.loads(task.worker_fence)
        assert fence == {
            "run_id": claimed.current_run_id,
            "claim_lock": claimed.claim_lock,
            "host": kb._host_id(),
            "leader_pid": pending.identity.pid,
            "worker_pgid": pending.identity.pgid,
            "worker_identity": pending.identity.token,
            "reason": "running",
            "created_at": fence["created_at"],
        }
        pending.release()
    finally:
        if pending is not None:
            pending.abort()
        conn.close()


def test_default_spawn_returns_blocked_pending_worker(isolated_home):
    conn = kb.connect()
    pending = None
    before = set(os.listdir("/dev/fd"))
    try:
        task_id = kb.create_task(conn, title="default blocked", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        workspace = kb.resolve_workspace(claimed)
        pending = kb._default_spawn(claimed, str(workspace))
        assert isinstance(pending, kb.PendingWorkerProcess)
        assert pending.proc.poll() is None
        assert pending.proc.args[:3] == [
            os.sys.executable,
            "-m",
            "hermes_cli.process_bootstrap",
        ]
        assert "--gate-fd" in pending.proc.args
        assert "--" in pending.proc.args
    finally:
        if pending is not None:
            pending.abort()
        conn.close()
    assert set(os.listdir("/dev/fd")) - before == set()


def test_bind_pending_worker_lost_claim_rolls_back_both_rows(
    isolated_home,
    tmp_path,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="bind lost", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock='fixture:new' WHERE id=?",
            (claimed.current_run_id,),
        )
        conn.commit()
        assert not kb._bind_pending_worker(
            conn,
            task_id,
            pending,
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        assert task.worker_fence is None and run.worker_fence is None
        assert task.worker_pid is None and run.worker_pid is None
    finally:
        if pending is not None:
            pending.abort()
        conn.close()


def test_bind_pending_worker_rejects_changed_process_identity(
    isolated_home,
    tmp_path,
    monkeypatch,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="identity changed", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        monkeypatch.setattr(kb, "_identity_matches", lambda _identity: False)

        assert not kb._bind_pending_worker(
            conn,
            task_id,
            pending,
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        task = kb.get_task(conn, task_id)
        run = kb.get_run(conn, claimed.current_run_id)
        assert task.worker_fence is None and run.worker_fence is None
        assert task.worker_pid is None and run.worker_pid is None
    finally:
        if pending is not None:
            monkeypatch.undo()
            pending.abort()
        conn.close()


def test_failed_bind_fences_only_losing_run_then_aborts_and_reaps(
    isolated_home,
    tmp_path,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="failed bind", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        now = int(time.time())
        new_run = conn.execute(
            "INSERT INTO task_runs(task_id,status,claim_lock,started_at) "
            "VALUES(?, 'running', 'fixture:new', ?)",
            (task_id, now),
        ).lastrowid
        conn.execute(
            "UPDATE tasks SET claim_lock='fixture:new', current_run_id=? WHERE id=?",
            (new_run, task_id),
        )
        conn.commit()
        raw = kb._record_and_abort_failed_bind(
            conn,
            task_id,
            pending,
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        assert pending.proc.poll() is not None
        task = kb.get_task(conn, task_id)
        assert task.current_run_id == new_run
        assert task.claim_lock == "fixture:new"
        assert task.worker_fence is None
        old_run = kb.get_run(conn, claimed.current_run_id)
        assert old_run.worker_fence == raw
        assert json.loads(raw)["reason"] == "spawn_bind_failed"
        assert kb.reap_terminal_attempt_fences(conn, limit=16) == [
            ("run", claimed.current_run_id)
        ]
        assert kb.get_run(conn, claimed.current_run_id).worker_fence is None
        assert kb.get_task(conn, task_id).current_run_id == new_run
    finally:
        if pending is not None:
            pending.abort()
        conn.close()


def test_failed_bind_claim_only_loss_is_reapable_without_task_contamination(
    isolated_home,
    tmp_path,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="claim-only loss", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        conn.execute(
            "UPDATE tasks SET claim_lock='fixture:new' WHERE id=?",
            (task_id,),
        )
        conn.commit()

        raw = kb._record_and_abort_failed_bind(
            conn,
            task_id,
            pending,
            run_id=claimed.current_run_id,
            claim_lock=claimed.claim_lock,
        )
        task = kb.get_task(conn, task_id)
        assert task.current_run_id == claimed.current_run_id
        assert task.claim_lock == "fixture:new"
        assert task.worker_fence is None
        assert kb.get_run(conn, claimed.current_run_id).worker_fence == raw
        assert kb.reap_terminal_attempt_fences(conn, limit=16) == [
            ("run", claimed.current_run_id)
        ]
        assert kb.get_task(conn, task_id).claim_lock == "fixture:new"
    finally:
        if pending is not None:
            pending.abort()
        conn.close()


def test_failed_bind_orphan_cas_loss_still_aborts_and_raises(
    isolated_home,
    tmp_path,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="orphan cas lost", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        conn.execute(
            "UPDATE task_runs SET claim_lock='fixture:raced' WHERE id=?",
            (claimed.current_run_id,),
        )
        conn.commit()
        with pytest.raises(kb.SpawnBindError):
            kb._record_and_abort_failed_bind(
                conn,
                task_id,
                pending,
                run_id=claimed.current_run_id,
                claim_lock=claimed.claim_lock,
            )
        assert pending.proc.poll() is not None
    finally:
        if pending is not None:
            pending.abort()
        conn.close()


def test_failed_bind_fence_construction_error_still_aborts(
    isolated_home,
    tmp_path,
    monkeypatch,
):
    conn = kb.connect()
    pending = None
    try:
        task_id = kb.create_task(conn, title="no host", assignee="dor-coo")
        claimed = kb.claim_task(conn, task_id, claimer="fixture:old")
        assert claimed is not None
        pending = kb._spawn_behind_bootstrap(
            ["/bin/sleep", "60"],
            env=os.environ,
            cwd=str(tmp_path),
            stdout=subprocess.DEVNULL,
        )
        monkeypatch.setattr(kb, "_host_id", lambda: None)
        with pytest.raises(kb.AttemptFenceCapabilityError):
            kb._record_and_abort_failed_bind(
                conn,
                task_id,
                pending,
                run_id=claimed.current_run_id,
                claim_lock=claimed.claim_lock,
            )
        assert pending.proc.poll() is not None
        assert pending.gate_write_fd == -1
    finally:
        monkeypatch.undo()
        if pending is not None:
            pending.abort()
        conn.close()
