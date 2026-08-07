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
    SessionCompressionLineageResolver,
    WorkerIdentity,
    pid_is_alive,
)
from hermes_state import SessionDB

FIXTURE = Path(__file__).parents[1] / "fixtures" / "worker_lifecycle_fixture.py"
_FAKE_BIRTH_TOKENS: dict[int, str] = {}


@pytest.fixture(autouse=True)
def _fake_native_birth_tokens(monkeypatch):
    """Resolve only PIDs explicitly registered by synthetic process doubles."""

    real_process_birth_token = ws.process_birth_token

    def resolve(pid: int):
        return _FAKE_BIRTH_TOKENS[int(pid)] if int(pid) in _FAKE_BIRTH_TOKENS else real_process_birth_token(pid)

    _FAKE_BIRTH_TOKENS.clear()
    monkeypatch.setattr(ws, "process_birth_token", resolve)
    yield
    _FAKE_BIRTH_TOKENS.clear()


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


def _wait_for(predicate, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _FakeProc:
    def __init__(self, pid: int, *, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = None
        self._configured_returncode = returncode
        self.terminated = False
        self.killed = False
        self._hermes_process_birth_token = f"test-birth-{pid}"
        _FAKE_BIRTH_TOKENS[pid] = self._hermes_process_birth_token

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._configured_returncode
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


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
            "UPDATE tasks SET status='running', session_id=?, workspace_path=?, current_run_id=1 WHERE id=?",
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
    def launch(
        identity: WorkerIdentity,
        attempt: int,
        event_path: Path,
        *,
        start_nonce: str,
    ):
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
                "--run-id",
                str(identity.run_id),
                "--outcome",
                outcome,
                f"--start-nonce={start_nonce}",
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
    # token_urlsafe() encodes this prefix as "-Pj4"; opaque nonces must remain
    # valid when transported through the fixture command line.
    monkeypatch.setattr(ws.secrets, "token_bytes", lambda size: b"\xf8" * size)
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
        WorkerIdentity(task_id, "session-stable", worktree, run_id=1),
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
    assert [(item["observed_session_id"], item["worktree"]) for item in identities] == [
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
        WorkerIdentity(task_id, "session-stable", worktree, run_id=1),
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
        start_nonce: str | None = None,
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
            f"--start-nonce={start_nonce}",
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

    def tracking_popen(*args, **kwargs):
        proc = popen_type(*args, **kwargs)
        launched.append(proc)
        original_wait = proc.wait

        def tracking_wait(*wait_args, **wait_kwargs):
            waited.append(proc)
            return original_wait(*wait_args, **wait_kwargs)

        proc.wait = tracking_wait
        return proc

    monkeypatch.setattr(subprocess, "Popen", tracking_popen)
    (tmp_path / "hermes" / "profiles" / "fixture").mkdir(parents=True)
    monkeypatch.setattr(
        kb,
        "_resolve_hermes_argv",
        lambda: [sys.executable, str(FIXTURE), "owned-default"],
    )
    monkeypatch.setattr(kb, "_resolve_worker_cli_toolsets", lambda _home: None)
    supervisor = kb._dispatcher_worker_supervisor()
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="supervised", assignee="fixture")
        result = kb.dispatch_once(conn)
        assert result.spawned[0][0] == task_id
        assert len(launched) == 1
        proc = launched[0]
        assert isinstance(proc, popen_type)
        assert proc.poll() is None
        task = kb.get_task(conn, task_id)
        worker_pid = task.worker_pid
        assert worker_pid == supervisor.active_pid(task_id, task.current_run_id)
    finally:
        conn.close()

    deadline = time.monotonic() + 5
    while supervisor.active_count and time.monotonic() < deadline:
        time.sleep(0.01)
    assert waited == [proc]
    assert supervisor.active_count == 0
    assert kb._classify_worker_exit(worker_pid) == ("clean_exit", 0)


def test_raw_rate_limit_exit_does_not_enter_transient_provider_retry(tmp_path):
    identity = WorkerIdentity("task-rate-limit", "session-rate-limit", tmp_path, run_id=1)

    class RateLimitedProc:
        returncode = 75

        def __init__(self, pid):
            self.pid = pid
            self._hermes_process_birth_token = f"test-birth-{pid}"
            _FAKE_BIRTH_TOKENS[pid] = self._hermes_process_birth_token

        def poll(self):
            return None

        def wait(self):
            return self.returncode

    raw_path = tmp_path / "raw.jsonl"
    typed_path = tmp_path / "typed.jsonl"
    raw_proc = RateLimitedProc(45101)
    typed_proc = RateLimitedProc(45102)
    _write_start_identity(raw_path, identity, 1, raw_proc.pid, "raw-nonce")
    _write_bound_event(
        typed_path, identity, 1, typed_proc.pid,
        classification="transient_provider",
        start_nonce="typed-nonce",
    )
    raw_exit = ws._attempt_exit(
        identity, 1, raw_proc, raw_path,
        _ownership(raw_proc, "raw-nonce"),
    )
    typed_exit = ws._attempt_exit(
        identity, 1, typed_proc, typed_path,
        _ownership(typed_proc, "typed-nonce"),
    )
    assert (raw_exit.exit_code, raw_exit.classification) == (75, "process_exit")
    assert (typed_exit.exit_code, typed_exit.classification) == (75, "transient_provider")

    launches: list[int] = []
    exits: list = []

    def launch(_identity, attempt, event_path, *, start_nonce):
        launches.append(attempt)
        proc = RateLimitedProc(45200 + attempt)
        _write_start_identity(event_path, identity, attempt, proc.pid, start_nonce)
        return proc

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

def _exact_identity(tmp_path, *, run_id=7):
    return WorkerIdentity(
        task_id="task_exact",
        run_id=run_id,
        session_id="worker_session",
        worktree=tmp_path / "worktree",
    )


def _ownership(proc, start_nonce, *, root_pid=None, root_birth_token=None):
    root_pid = proc.pid if root_pid is None else root_pid
    return ws._AttemptOwnership(
        nonce=start_nonce,
        launcher_pid=proc.pid,
        launcher_birth_token=proc._hermes_process_birth_token,
        root_pid=root_pid,
        root_birth_token=(
            f"test-birth-{root_pid}"
            if root_birth_token is None
            else root_birth_token
        ),
    )


def _write_start_identity(path, identity, attempt, pid, start_nonce):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 3,
        "kind": "identity",
        "nonce": start_nonce,
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "attempt": attempt,
        "expected_session_id": identity.session_id,
        "observed_session_id": identity.session_id,
        "worktree": str(identity.worktree.resolve()),
        "root_pid": pid,
        "process_birth_token": f"test-birth-{pid}",
    }
    path.write_text(json.dumps(payload) + chr(10), encoding="utf-8")
    return payload


def _write_bound_event(
    path,
    identity,
    attempt,
    pid,
    *,
    classification,
    start_nonce="test-start-nonce",
):
    _write_start_identity(path, identity, attempt, pid, start_nonce)
    payload = {
        "schema_version": 3,
        "kind": "terminal",
        "nonce": start_nonce,
        "task_id": identity.task_id,
        "run_id": identity.run_id,
        "attempt": attempt,
        "expected_session_id": identity.session_id,
        "observed_session_id": identity.session_id,
        "worktree": str(identity.worktree.resolve()),
        "root_pid": pid,
        "process_birth_token": f"test-birth-{pid}",
        "exit_kind": "code",
        "exit_value": 75 if classification == "transient_provider" else 0,
        "failure_reason": "transient_provider" if classification == "transient_provider" else "none",
        "classification": classification,
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload) + chr(10))
    return payload


_MISSING = object()


def _classified_event(
    tmp_path,
    *,
    returncode,
    classification,
    mutate=None,
    duplicate=False,
    identity=None,
    lineage_resolver=None,
):
    identity = identity or _exact_identity(tmp_path)
    proc = _FakeProc(7601, returncode=returncode)
    path = tmp_path / "closed-event.jsonl"
    nonce = "classified-nonce"
    payload = _write_bound_event(
        path,
        identity,
        1,
        proc.pid,
        classification=classification,
        start_nonce=nonce,
    )
    if mutate is not None:
        if isinstance(mutate, dict):
            payload.update(mutate)
        else:
            field, replacement = mutate
            if replacement is _MISSING:
                payload.pop(field, None)
            else:
                payload[field] = replacement
        identity_row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        path.write_text(
            json.dumps(identity_row) + chr(10) + json.dumps(payload) + chr(10),
            encoding="utf-8",
        )
    if duplicate:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload) + chr(10))
    proc.wait()
    return ws._attempt_exit(
        identity,
        1,
        proc,
        path,
        _ownership(proc, nonce),
        lineage_resolver=lineage_resolver,
    )


def _persist_compression_chain(db: SessionDB, *session_ids: str) -> None:
    for index, session_id in enumerate(session_ids):
        parent_id = session_ids[index - 1] if index else None
        db.create_session(session_id, source="cli", parent_session_id=parent_id)
        if parent_id is not None:
            db.end_session(parent_id, "compression")


def test_terminal_evidence_accepts_exact_root_with_real_session_db(tmp_path):
    identity = _exact_identity(tmp_path)
    db_path = tmp_path / "profile" / "state.db"
    db = SessionDB(db_path)
    try:
        db.create_session(identity.session_id, source="cli")
    finally:
        db.close()

    observed = _classified_event(
        tmp_path,
        returncode=0,
        classification="success",
        lineage_resolver=SessionCompressionLineageResolver(db_path),
    )

    assert observed.classification == "success"


@pytest.mark.parametrize("observed_session_id", ["successor", "tip"])
def test_terminal_evidence_accepts_persisted_compression_descendant(
    tmp_path, observed_session_id,
):
    identity = _exact_identity(tmp_path)
    db_path = tmp_path / "profile" / "state.db"
    db = SessionDB(db_path)
    try:
        _persist_compression_chain(db, identity.session_id, "successor", "tip")
    finally:
        db.close()

    observed = _classified_event(
        tmp_path,
        returncode=0,
        classification="success",
        mutate=("observed_session_id", observed_session_id),
        lineage_resolver=SessionCompressionLineageResolver(db_path),
    )

    assert observed.classification == "success"


@pytest.mark.parametrize(
    "case",
    [
        "sibling",
        "ancestor",
        "unrelated",
        "merely_existing",
        "missing_db",
        "malformed_db",
        "ambiguous",
    ],
)
def test_terminal_evidence_rejects_unproven_compression_lineage(tmp_path, case):
    db_path = tmp_path / "profile" / "state.db"
    identity = _exact_identity(tmp_path)
    observed_session_id = "observed"

    if case == "missing_db":
        resolver = SessionCompressionLineageResolver(db_path)
    elif case == "malformed_db":
        db_path.parent.mkdir(parents=True)
        db_path.write_text("not sqlite", encoding="utf-8")
        resolver = SessionCompressionLineageResolver(db_path)
    else:
        db = SessionDB(db_path)
        try:
            if case == "ancestor":
                _persist_compression_chain(db, "observed", identity.session_id)
            elif case == "unrelated":
                db.create_session(identity.session_id, source="cli")
                db.create_session("observed", source="cli")
            elif case == "merely_existing":
                db.create_session(identity.session_id, source="cli")
                db.create_session(
                    "observed", source="cli", parent_session_id=identity.session_id,
                )
            else:
                _persist_compression_chain(db, identity.session_id, "successor")
                if case == "sibling":
                    db.create_session(
                        "observed",
                        source="cli",
                        parent_session_id=identity.session_id,
                        model_config={"_branched_from": identity.session_id},
                    )
                else:
                    db.create_session(
                        "observed", source="cli", parent_session_id=identity.session_id,
                    )
        finally:
            db.close()
        resolver = SessionCompressionLineageResolver(db_path)

    observed = _classified_event(
        tmp_path,
        returncode=0,
        classification="success",
        mutate=("observed_session_id", observed_session_id),
        identity=identity,
        lineage_resolver=resolver,
    )

    assert observed.classification == "invalid_evidence"


def test_start_identity_still_rejects_compression_successor(tmp_path):
    identity = _exact_identity(tmp_path)
    event = _write_start_identity(
        tmp_path / "start.jsonl", identity, 1, 7601, "start-nonce",
    )
    event["observed_session_id"] = "successor"

    assert ws._start_identity_matches(event, identity, 1, "start-nonce") is False


def test_terminal_evidence_rejects_forged_expected_session_env(tmp_path):
    identity = _exact_identity(tmp_path)
    db_path = tmp_path / "profile" / "state.db"
    db = SessionDB(db_path)
    try:
        _persist_compression_chain(db, identity.session_id, "successor")
    finally:
        db.close()

    observed = _classified_event(
        tmp_path,
        returncode=0,
        classification="success",
        mutate={
            "expected_session_id": "successor",
            "observed_session_id": "successor",
        },
        identity=identity,
        lineage_resolver=SessionCompressionLineageResolver(db_path),
    )

    assert observed.classification == "invalid_evidence"


@pytest.mark.parametrize(
    ("returncode", "classification"),
    [(0, "success"), (75, "transient_provider")],
)
def test_closed_terminal_evidence_accepts_valid_completed_and_provider_failure(
    tmp_path, returncode, classification,
):
    observed = _classified_event(
        tmp_path, returncode=returncode, classification=classification,
    )
    assert observed.classification == classification


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("kind", "unknown_event"),
        ("kind", _MISSING),
        ("classification", "unknown_classification"),
        ("classification", _MISSING),
        ("failure_reason", "unknown_reason"),
        ("failure_reason", _MISSING),
        ("exit_kind", "unknown_exit"),
        ("exit_kind", _MISSING),
    ],
)
def test_closed_terminal_evidence_rejects_unknown_or_missing_enums(
    tmp_path, field, replacement,
):
    observed = _classified_event(
        tmp_path,
        returncode=75,
        classification="transient_provider",
        mutate=(field, replacement),
    )
    assert observed.classification == "invalid_evidence"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", _MISSING),
        ("task_id", _MISSING),
        ("run_id", _MISSING),
        ("attempt", _MISSING),
        ("expected_session_id", _MISSING),
        ("observed_session_id", _MISSING),
        ("worktree", _MISSING),
        ("root_pid", _MISSING),
        ("process_birth_token", _MISSING),
        ("exit_value", _MISSING),
        ("schema_version", 1),
        ("task_id", "other_task"),
        ("run_id", 8),
        ("attempt", 2),
        ("expected_session_id", "other_session"),
        ("observed_session_id", "other_session"),
        ("worktree", "other/worktree"),
        ("root_pid", 7602),
        ("process_birth_token", "other-birth"),
        ("exit_value", 76),
    ],
)
def test_closed_terminal_evidence_requires_exact_attempt_identity(
    tmp_path, field, replacement,
):
    observed = _classified_event(
        tmp_path,
        returncode=75,
        classification="transient_provider",
        mutate=(field, replacement),
    )
    assert observed.classification == "invalid_evidence"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("classification", "success"),
        ("failure_reason", "none"),
        ("failure_reason", "billing"),
        ("failure_reason", "supervisor_failure"),
        ("failure_reason", "ownership_loss"),
        ("exit_kind", "signal"),
    ],
)
def test_closed_terminal_evidence_rejects_inconsistent_recoverable_claims(
    tmp_path, field, replacement,
):
    observed = _classified_event(
        tmp_path,
        returncode=75,
        classification="transient_provider",
        mutate=(field, replacement),
    )
    assert observed.classification == "invalid_evidence"


def test_closed_terminal_evidence_rejects_provider_failure_with_success_exit(tmp_path):
    observed = _classified_event(
        tmp_path, returncode=0, classification="transient_provider",
    )
    assert observed.classification == "invalid_evidence"


def test_closed_terminal_evidence_rejects_duplicate_terminal_record(tmp_path):
    observed = _classified_event(
        tmp_path,
        returncode=75,
        classification="transient_provider",
        duplicate=True,
    )
    assert observed.classification == "invalid_evidence"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_id", "other_task"),
        ("run_id", 8),
        ("attempt", 2),
        ("expected_session_id", "other_session"),
        ("observed_session_id", "other_session"),
        ("worktree", "other/worktree"),
    ],
)
def test_transient_terminal_evidence_rejects_identity_mismatch(
    tmp_path, field, replacement,
):
    identity = _exact_identity(tmp_path)
    launches = []
    notices = []

    def launch(_identity, attempt, event_path, *, start_nonce):
        launches.append(attempt)
        proc = _FakeProc(7000 + attempt, returncode=75)
        _write_bound_event(
            event_path, identity, attempt, proc.pid,
            classification="transient_provider",
            start_nonce=start_nonce,
        )
        rows = [json.loads(line) for line in event_path.read_text().splitlines()]
        rows[-1][field] = replacement
        event_path.write_text(
            "".join(json.dumps(row) + chr(10) for row in rows),
            encoding="utf-8",
        )
        return proc

    supervisor = DispatcherWorkerSupervisor(event_root=tmp_path / "events")
    handle = supervisor.start(identity, launch=launch, notifier=notices.append)
    assert handle.wait(2)
    assert launches == [1]
    assert len(notices) == 1
    assert supervisor.active_count == 0


def test_exact_fresh_recovery_signal_releases_one_resume_and_no_third(tmp_path):
    identity = _exact_identity(tmp_path)
    launches = []
    exits = []
    gates = []

    def launch(_identity, attempt, event_path, *, start_nonce):
        launches.append(attempt)
        classification = "transient_provider" if attempt == 1 else "success"
        proc = _FakeProc(7100 + attempt, returncode=75 if attempt == 1 else 0)
        _write_bound_event(
            event_path, identity, attempt, proc.pid,
            classification=classification,
            start_nonce=start_nonce,
        )
        return proc

    supervisor = DispatcherWorkerSupervisor(
        event_root=tmp_path / "events", recovery_timeout=1,
    )
    handle = supervisor.start(
        identity,
        launch=launch,
        on_exit=exits.append,
        gate_advance=lambda _identity: gates.append("advance"),
    )
    assert supervisor.signal_recovery(identity, "too-early", signaled_at=0) is False
    assert _wait_for(lambda: supervisor.is_waiting_for_recovery(identity), timeout=2)
    stale = WorkerIdentity(
        task_id=identity.task_id,
        run_id=identity.run_id + 1,
        session_id=identity.session_id,
        worktree=identity.worktree,
    )
    assert supervisor.signal_recovery(stale, "wrong-run") is False
    assert supervisor.signal_recovery(identity, "fresh") is True
    assert handle.wait(2)
    assert launches == [1, 2]
    assert [item.attempt for item in exits] == [1, 2]
    assert gates == ["advance"]
    assert supervisor.signal_recovery(identity, "third") is False


def test_pid_projection_failure_cleans_and_notifies_once(tmp_path, monkeypatch):
    identity = _exact_identity(tmp_path)
    killed = []
    notices = []

    class RunningProc(_FakeProc):
        def poll(self):
            return None

        def wait(self, timeout=None):
            self.returncode = -15
            return self.returncode

    proc = RunningProc(7201)

    def cleanup_failed_start(item, timeout):
        killed.append(item)

    monkeypatch.setattr(ws, "_cleanup_failed_start", cleanup_failed_start)

    def bad_projection(_identity, _attempt, _pid):
        raise RuntimeError("projection failed")

    def launch(identity, attempt, event_path, *, start_nonce):
        _write_start_identity(event_path, identity, attempt, proc.pid, start_nonce)
        return proc

    supervisor = DispatcherWorkerSupervisor(event_root=tmp_path / "events")
    with pytest.raises(RuntimeError, match="projection failed"):
        supervisor.start(
            identity,
            launch=launch,
            on_pid=bad_projection,
            notifier=notices.append,
        )
    assert killed == [proc]
    assert len(notices) == 1
    assert supervisor.active_count == 0
    assert supervisor.active_pid(identity.task_id, identity.run_id) is None
