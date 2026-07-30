"""Deterministic recovery proof for dispatcher-owned worker subprocesses."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import worker_supervisor as ws
from hermes_cli.worker_supervisor import (
    DispatcherWorkerSupervisor,
    WorkerIdentity,
    pid_is_alive,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "worker_lifecycle_fixture.py"


def _init_git_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Hermes Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-qm",
            "fixture base",
        ],
        cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-qb", "fixture-recovery", str(worktree)],
        cwd=repo,
        check=True,
    )
    return repo, worktree


def _listener_closed(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _build_board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    board_db = tmp_path / "board" / "kanban.db"
    board_db.parent.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(conn, title="recover worker", assignee="fixture")
    next_id = kb.create_task(conn, title="serial gate 2", assignee="fixture")
    final_id = kb.create_task(conn, title="serial gate 3", assignee="fixture")
    kb.link_tasks(conn, task_id, next_id)
    kb.link_tasks(conn, next_id, final_id)
    kb.recompute_ready(conn)
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='running', session_id=?, workspace_path=? WHERE id=?",
            ("session-stable", str(tmp_path / "worktree"), task_id),
        )
    return conn, board_db, task_id, next_id, final_id


def _persist_exit(board_db: Path, task_id: str, observed: list, event: threading.Event):
    def callback(attempt_exit) -> None:
        observed.append(attempt_exit)
        with sqlite3.connect(board_db) as conn:
            conn.execute(
                "INSERT INTO task_events(task_id, kind, payload, created_at) "
                "VALUES (?, 'worker_attempt_exit', ?, strftime('%s','now'))",
                (task_id, json.dumps(attempt_exit.as_dict(), sort_keys=True)),
            )
        event.set()

    return callback


def _launcher(board_db: Path, task_id: str, outcome: str, launches: list[dict]):
    def launch(identity: WorkerIdentity, attempt: int, event_path: Path):
        if attempt == 2:
            first = launches[0]
            assert not pid_is_alive(first["listener_pid"])
            assert _listener_closed(first["listener_port"])
        proc = subprocess.Popen(
            [
                sys.executable,
                str(FIXTURE),
                "attempt",
                "--event-path",
                str(event_path),
                "--attempt",
                str(attempt),
                "--session-id",
                identity.session_id,
                "--worktree",
                str(identity.worktree),
                "--board-db",
                str(board_db),
                "--task-id",
                task_id,
                "--outcome",
                outcome,
            ],
            cwd=identity.worktree,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        launches.append({"attempt": attempt, "pid": proc.pid})
        return proc

    return launch


def _capture_first_owned(observed: list, launches: list[dict]) -> None:
    first = observed[0]
    listener = next(resource for resource in first.owned_processes if resource.role == "listener")
    launches[0]["listener_pid"] = listener.pid
    launches[0]["listener_port"] = listener.port


def test_transient_provider_recovers_once_and_advances_one_gate(monkeypatch, tmp_path):
    _, worktree = _init_git_worktree(tmp_path)
    conn, board_db, task_id, next_id, final_id = _build_board(monkeypatch, tmp_path)
    exits: list = []
    launches: list[dict] = []
    first_exit = threading.Event()
    notifications: list = []

    def on_exit(attempt_exit) -> None:
        _persist_exit(board_db, task_id, exits, first_exit)(attempt_exit)
        if attempt_exit.attempt == 1:
            _capture_first_owned(exits, launches)

    def on_success(_attempt_exit) -> None:
        with kb.connect() as gate_conn:
            kb.recompute_ready(gate_conn)

    supervisor = DispatcherWorkerSupervisor(
        event_root=tmp_path / "events",
        recovery_timeout=5,
        cleanup_timeout=5,
    )
    handle = supervisor.start(
        WorkerIdentity(task_id, "session-stable", worktree),
        _launcher(board_db, task_id, "success", launches),
        on_exit=on_exit,
        on_success=on_success,
        notifier=notifications.append,
    )

    assert first_exit.wait(5)
    assert exits[0].exit_code == 75
    assert exits[0].classification == "transient_provider"
    handle.signal_recovery("credential_recovered")
    assert handle.wait(10)

    assert [item["attempt"] for item in launches] == [1, 2]
    assert [item.classification for item in exits] == ["transient_provider", "success"]
    identities = [
        event
        for item in exits
        for event in item.events
        if event.get("kind") == "identity"
    ]
    assert [(item["session_id"], item["worktree"]) for item in identities] == [
        ("session-stable", str(worktree)),
        ("session-stable", str(worktree)),
    ]
    assert notifications == []
    assert supervisor.active_count == 0
    assert all(not pid_is_alive(item["pid"]) for item in launches)
    assert not pid_is_alive(launches[0]["listener_pid"])
    assert _listener_closed(launches[0]["listener_port"])
    assert (worktree / "recovered.txt").read_text(encoding="utf-8") == "recovered\n"
    assert subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "fixture: recovered worker"
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, next_id).status == "ready"
    assert kb.get_task(conn, final_id).status == "todo"
    persisted = [event for event in kb.list_events(conn, task_id) if event.kind == "worker_attempt_exit"]
    assert len(persisted) == 2
    assert persisted[0].payload["exit_code"] == 75
    assert persisted[0].payload["classification"] == "transient_provider"


def test_transient_provider_retry_failure_notifies_once(monkeypatch, tmp_path):
    _, worktree = _init_git_worktree(tmp_path)
    conn, board_db, task_id, next_id, final_id = _build_board(monkeypatch, tmp_path)
    exits: list = []
    launches: list[dict] = []
    first_exit = threading.Event()
    notifications: list = []

    def on_exit(attempt_exit) -> None:
        _persist_exit(board_db, task_id, exits, first_exit)(attempt_exit)
        if attempt_exit.attempt == 1:
            _capture_first_owned(exits, launches)

    supervisor = DispatcherWorkerSupervisor(
        event_root=tmp_path / "events",
        recovery_timeout=5,
        cleanup_timeout=5,
    )
    handle = supervisor.start(
        WorkerIdentity(task_id, "session-stable", worktree),
        _launcher(board_db, task_id, "fail", launches),
        on_exit=on_exit,
        notifier=notifications.append,
    )

    assert first_exit.wait(5)
    handle.signal_recovery("credential_recovered")
    assert handle.wait(10)

    assert [item["attempt"] for item in launches] == [1, 2]
    assert [item.exit_code for item in exits] == [75, 76]
    assert len(notifications) == 1
    assert notifications[0].task_id == task_id
    assert notifications[0].attempts == 2
    assert notifications[0].classification == "transient_provider"
    assert supervisor.active_count == 0
    assert all(not pid_is_alive(item["pid"]) for item in launches)
    assert not pid_is_alive(launches[0]["listener_pid"])
    assert _listener_closed(launches[0]["listener_port"])
    assert kb.get_task(conn, task_id).status == "running"
    assert kb.get_task(conn, next_id).status == "todo"
    assert kb.get_task(conn, final_id).status == "todo"


def test_dispatch_once_production_path_recovers_once_and_advances_one_serial_gate(
    monkeypatch, tmp_path
):
    _, worktree = _init_git_worktree(tmp_path)
    board_db = tmp_path / "board" / "kanban.db"
    board_db.parent.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._dispatcher_worker_supervisors.clear()

    launches: list[dict] = []
    waited: list[subprocess.Popen] = []
    first_listener: dict[str, int] = {}

    def fixture_default_spawn(
        task: kb.Task,
        workspace: str,
        *,
        board: str | None = None,
        lifecycle_event_path: Path | None = None,
        lifecycle_attempt: int = 1,
    ):
        assert lifecycle_event_path is not None
        if lifecycle_attempt == 2:
            assert not pid_is_alive(first_listener["pid"])
            assert _listener_closed(first_listener["port"])
        command = [
            sys.executable,
            str(FIXTURE),
            "attempt",
            "--event-path",
            str(lifecycle_event_path),
            "--attempt",
            str(lifecycle_attempt),
            "--task-id",
            task.id,
            "--run-id",
            str(task.current_run_id),
            "--session-id",
            str(task.session_id),
            "--worktree",
            workspace,
            "--board-db",
            str(board_db),
            "--outcome",
            "success",
        ]
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        original_wait = proc.wait

        def tracking_wait(*args, **kwargs):
            waited.append(proc)
            return original_wait(*args, **kwargs)

        proc.wait = tracking_wait
        launches.append(
            {
                "attempt": lifecycle_attempt,
                "proc": proc,
                "task_id": task.id,
                "run_id": task.current_run_id,
                "session_id": task.session_id,
                "worktree": workspace,
                "board": board,
            }
        )
        return proc

    monkeypatch.setattr(kb, "_default_spawn", fixture_default_spawn)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="serial gate 1",
            assignee="fixture",
            workspace_kind="dir",
            workspace_path=str(worktree),
            session_id="session-stable",
        )
        next_id = kb.create_task(
            conn, title="serial gate 2", assignee="fixture"
        )
        final_id = kb.create_task(
            conn, title="serial gate 3", assignee="fixture"
        )
        kb.link_tasks(conn, task_id, next_id)
        kb.link_tasks(conn, next_id, final_id)
        kb.recompute_ready(conn)

        result = kb.dispatch_once(conn)
        assert [item[0] for item in result.spawned] == [task_id]
        run_id = kb.get_task(conn, task_id).current_run_id
        assert run_id is not None

        deadline = time.monotonic() + 5
        persisted = []
        while time.monotonic() < deadline:
            persisted = [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "worker_attempt_exit"
            ]
            if persisted:
                break
            time.sleep(0.01)
        assert len(persisted) == 1
        assert (
            persisted[0].payload["exit_code"],
            persisted[0].payload["classification"],
        ) == (75, "transient_provider")
        listener = next(
            item
            for item in persisted[0].payload["owned_processes"]
            if item["role"] == "listener"
        )
        first_listener.update(pid=listener["pid"], port=listener["port"])

        kb.dispatch_once(conn)
        task = kb.get_task(conn, task_id)
        assert (task.status, task.current_run_id) == ("running", run_id)
        assert not any(
            event.kind in {"rate_limited", "crashed"}
            for event in kb.list_events(conn, task_id)
        )
        assert len(launches) == 1

        assert kb.signal_worker_recovery(task_id, "credential_recovered")
        deadline = time.monotonic() + 10
        supervisor = kb._dispatcher_worker_supervisors[str(board_db.resolve())]
        while supervisor.active_count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert supervisor.active_count == 0

        assert len(launches) == 2
        assert [item["attempt"] for item in launches] == [1, 2]
        assert [
            (item["task_id"], item["run_id"], item["session_id"], item["worktree"])
            for item in launches
        ] == [
            (task_id, run_id, "session-stable", str(worktree)),
            (task_id, run_id, "session-stable", str(worktree)),
        ]
        assert waited[0] is launches[0]["proc"]
        assert all(not pid_is_alive(item["proc"].pid) for item in launches)
        assert not pid_is_alive(first_listener["pid"])
        assert _listener_closed(first_listener["port"])
        assert (worktree / "recovered.txt").read_text(encoding="utf-8") == 'recovered\n'
        assert [kb.get_task(conn, item).status for item in (task_id, next_id, final_id)] == [
            "done",
            "ready",
            "todo",
        ]
        persisted = [
            event.payload
            for event in kb.list_events(conn, task_id)
            if event.kind == "worker_attempt_exit"
        ]
        assert [(item["exit_code"], item["classification"]) for item in persisted] == [
            (75, "transient_provider"),
            (0, "success"),
        ]
    finally:
        conn.close()
        kb._dispatcher_worker_supervisors.clear()


def test_dispatch_once_default_spawn_is_owned_by_supervisor(monkeypatch, tmp_path):
    board_db = tmp_path / "board" / "kanban.db"
    board_db.parent.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "user-profile"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))
    kb.init_db()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _name: True)
    kb._dispatcher_worker_supervisors.clear()

    popen_type = subprocess.Popen
    launched: list[subprocess.Popen] = []
    waited: list[subprocess.Popen] = []
    default_spawn_returns: list[object] = []

    def tracking_popen(*args, **kwargs):
        proc = popen_type(*args, **kwargs)
        launched.append(proc)
        original_wait = proc.wait

        def tracking_wait(*wait_args, **wait_kwargs):
            waited.append(proc)
            return original_wait(*wait_args, **wait_kwargs)

        proc.wait = tracking_wait
        return proc

    original_default_spawn = kb._default_spawn

    def tracking_default_spawn(*args, **kwargs):
        proc = original_default_spawn(*args, **kwargs)
        default_spawn_returns.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    monkeypatch.setattr(
        kb,
        "_resolve_hermes_argv",
        lambda: [sys.executable, "-c", "import time; time.sleep(1)"],
    )
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    monkeypatch.setattr(kb, "_default_spawn", tracking_default_spawn)
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="supervised", assignee="fixture")
        result = kb.dispatch_once(conn)
        assert result.spawned[0][0] == task_id
        assert len(launched) == 1
        proc = launched[0]
        assert isinstance(proc, popen_type)
        assert proc.poll() is None
        assert default_spawn_returns == [proc]
        assert kb.get_task(conn, task_id).worker_pid == proc.pid
    finally:
        conn.close()

    deadline = time.monotonic() + 5
    supervisor = kb._dispatcher_worker_supervisor()
    while supervisor.active_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert waited == [proc]
    assert supervisor.active_count == 0
    assert kb._classify_worker_exit(proc.pid) == ("clean_exit", 0)


def test_raw_rate_limit_exit_does_not_enter_transient_provider_retry(tmp_path):
    identity = WorkerIdentity("task-rate-limit", "session-rate-limit", tmp_path)

    class RateLimitedProc:
        returncode = 75

        def __init__(self, pid):
            self.pid = pid

        def wait(self):
            return self.returncode

    raw_path = tmp_path / "raw.jsonl"
    typed_path = tmp_path / "typed.jsonl"
    typed_path.write_text(
        json.dumps({"kind": "failure", "classification": "transient_provider"}) + "\n",
        encoding="utf-8",
    )
    raw_exit = ws._attempt_exit(identity, 1, RateLimitedProc(45101), raw_path)
    typed_exit = ws._attempt_exit(identity, 1, RateLimitedProc(45102), typed_path)
    assert (raw_exit.exit_code, raw_exit.classification) == (75, "process_exit")
    assert (typed_exit.exit_code, typed_exit.classification) == (75, "transient_provider")

    launches: list[int] = []
    exits: list = []

    def launch(_identity, attempt, _event_path):
        launches.append(attempt)
        return RateLimitedProc(45200 + attempt)

    supervisor = DispatcherWorkerSupervisor(
        event_root=tmp_path / "events",
        recovery_timeout=1,
    )
    handle = supervisor.start(identity, launch, on_exit=exits.append)
    handle.signal_recovery("must-not-retry-rate-limit")
    assert handle.wait(2)
    assert launches == [1]
    assert [(item.exit_code, item.classification) for item in exits] == [(75, "process_exit")]
    assert supervisor.active_count == 0
