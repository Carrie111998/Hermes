"""Real process ownership handoff from AIAgent to durable Kanban runs."""
from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from run_agent import AIAgent
import tools.process_registry as pr
import utils


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    # Some test modules temporarily replace the hermes_cli package tree.  The
    # registry imports kanban_db when it settles a process, so make that
    # runtime import resolve to the same module this test patches.
    live_hermes_cli = importlib.import_module("hermes_cli")
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_db", kb)
    monkeypatch.setattr(live_hermes_cli, "kanban_db", kb, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _bare_agent(session_id):
    agent = object.__new__(AIAgent)
    agent.session_id = session_id
    return agent


def test_close_kills_ordinary_process_but_preserves_transferred_external_run(
    kanban_home, tmp_path, monkeypatch,
):
    registry = pr.ProcessRegistry()
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)

    ordinary = registry.spawn_local("sleep 30", task_id="agent-session")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="durable external", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="job-1")
    transferred = registry.spawn_local("sleep 30", task_id="agent-session")
    assert registry.transfer_to_external_run(transferred.id, task_id=task_id, run_id=run.id)

    _bare_agent("agent-session").close()

    assert ordinary.exited is True
    assert transferred.exited is False
    persisted = json.loads(checkpoint.read_text())
    row = next(x for x in persisted if x["session_id"] == transferred.id)
    assert row["kanban_task_id"] == task_id
    assert row["kanban_external_run_id"] == run.id
    assert row["external_run_owned"] is True

    restored = pr.ProcessRegistry()
    monkeypatch.setattr(restored, "_host_pid_is_ours", lambda pid, started: pid == transferred.pid)
    assert restored.recover_from_checkpoint() == 1
    recovered = restored._running[transferred.id]
    assert recovered.kanban_task_id == task_id
    assert recovered.kanban_external_run_id == run.id
    assert recovered.external_run_owned is True

    # Cleanup real survivor deliberately after all assertions.
    restored.kill_all(source="test_cleanup", include_external_owned=True)


def test_process_transfer_rejects_wrong_session_task_run_or_board_without_external_claim(
    kanban_home, tmp_path, monkeypatch,
):
    """Untrusted process/task/run/board identity never creates an external claim."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claimed worker", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("sleep 1", task_id="agent-session")

    rejected = json.loads(pr._handle_process({
        "action": "transfer", "session_id": "proc-not-the-worker", "external_id": "bad",
        "owner": "validator",
    }, task_id="agent-session"))
    assert "error" in rejected
    for name, value in (("HERMES_KANBAN_TASK", "t_wrong"), ("HERMES_KANBAN_RUN_ID", "999999"), ("HERMES_KANBAN_BOARD", "wrong-board")):
        monkeypatch.setenv(name, value)
        rejected = json.loads(pr._handle_process({
            "action": "transfer", "session_id": process.id, "external_id": f"bad-{name}",
            "owner": "validator",
        }, task_id="agent-session"))
        assert "error" in rejected
        monkeypatch.setenv(name, {"HERMES_KANBAN_TASK": task_id, "HERMES_KANBAN_RUN_ID": str(claimed.current_run_id), "HERMES_KANBAN_BOARD": "default"}[name])

    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id == claimed.current_run_id
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE owner_kind='external'").fetchone()[0] == 0
    registry.kill_all("agent-session", source="test_cleanup")


def test_process_transfer_rejects_nonworker_and_already_exited_process(
    kanban_home, tmp_path, monkeypatch,
):
    """A delegated caller or an exited process cannot strand an external claim."""
    from agent.delegation_context import non_dispatcher_owned_context

    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claimed worker", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("true", task_id="agent-session")
    process._completion_event.wait(5)

    with non_dispatcher_owned_context():
        rejected = json.loads(pr._handle_process({
            "action": "transfer", "session_id": process.id, "external_id": "bad", "owner": "validator",
        }, task_id="agent-session"))
    assert "error" in rejected
    rejected = json.loads(pr._handle_process({
        "action": "transfer", "session_id": process.id, "external_id": "bad", "owner": "validator",
    }, task_id="agent-session"))
    assert "error" in rejected
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id == claimed.current_run_id
        assert conn.execute("SELECT COUNT(*) FROM task_runs WHERE owner_kind='external'").fetchone()[0] == 0


def test_process_transfer_registry_race_settles_external_claim_for_retry(
    kanban_home, tmp_path, monkeypatch,
):
    """A process that disappears during registry transfer leaves no external claim."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="registry race", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("sleep 1", task_id="agent-session")
    monkeypatch.setattr(registry, "transfer_to_external_run", lambda *args, **kwargs: False)

    rejected = json.loads(pr._handle_process({
        "action": "transfer", "session_id": process.id, "external_id": "race-1", "owner": "validator",
        "on_failure": "retry",
    }, task_id="agent-session"))
    assert "error" in rejected
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute("SELECT status, outcome FROM task_runs WHERE owner_kind='external'").fetchone()
        assert task is not None and task.status == "running"
        assert task.current_run_id == claimed.current_run_id
        assert run is not None and (run["status"], run["outcome"]) == (
            "handoff_rolled_back", "handoff_rolled_back",
        )
    registry.kill_all("agent-session", source="test_cleanup")


@pytest.mark.parametrize("failure", ("write", "open", "fsync", "dir_fsync", "replace"))
def test_checkpoint_failure_fails_closed_and_restores_process_ownership(
    kanban_home, tmp_path, monkeypatch, failure,
):
    """No durable external run survives a transfer whose checkpoint cannot commit."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="checkpoint failure", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("sleep 2", task_id="agent-session")
    prior_ownership = (
        process.kanban_task_id, process.kanban_external_run_id, process.kanban_board,
        process.kanban_external_owner, process.external_run_owned,
    )

    def fail(*_args, **_kwargs):
        raise OSError(failure)

    if failure == "write":
        monkeypatch.setattr(utils.json, "dump", fail)
    elif failure == "open":
        monkeypatch.setattr(utils.os, "fdopen", fail)
    elif failure == "fsync":
        monkeypatch.setattr(utils.os, "fsync", fail)
    elif failure == "dir_fsync":
        original_fsync = utils.os.fsync
        fsync_calls = 0

        def fail_directory_fsync(fd):
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError(failure)
            return original_fsync(fd)

        monkeypatch.setattr(utils.os, "fsync", fail_directory_fsync)
    else:
        monkeypatch.setattr(utils, "atomic_replace", fail)
    rejected = json.loads(pr._handle_process({
        "action": "transfer", "session_id": process.id, "external_id": f"checkpoint-{failure}",
        "owner": "validator", "on_failure": "retry",
    }, task_id="agent-session"))
    assert "error" in rejected
    assert (
        process.kanban_task_id, process.kanban_external_run_id, process.kanban_board,
        process.kanban_external_owner, process.external_run_owned,
    ) == prior_ownership
    with kb.connect() as conn:
        runs = conn.execute(
            "SELECT status, outcome FROM task_runs WHERE owner_kind='external'"
        ).fetchall()
        assert len(runs) == 1
        assert (runs[0]["status"], runs[0]["outcome"]) == (
            "handoff_rolled_back", "handoff_rolled_back",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "running"
        assert task.current_run_id == claimed.current_run_id
    if failure == "dir_fsync":
        recovered = pr.ProcessRegistry()
        monkeypatch.setattr(pr, "process_registry", recovered)
        assert recovered.recover_from_checkpoint() == 1
        restored = recovered._running[process.id]
        assert restored.external_run_owned is False
        assert restored.kanban_external_run_id is None
        recovered.kill_all("agent-session", source="test_cleanup")
    else:
        registry.kill_all("agent-session", source="test_cleanup")


def test_persistent_directory_fsync_failure_keeps_external_claim_fail_closed(
    kanban_home, tmp_path, monkeypatch,
):
    """Never roll the DB back when restored checkpoint durability is unknown."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="persistent fsync", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("sleep 2", task_id="agent-session")

    original_fsync = utils.os.fsync
    fsync_calls = 0

    def fail_every_directory_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls % 2 == 0:
            raise OSError("persistent directory fsync")
        return original_fsync(fd)

    monkeypatch.setattr(utils.os, "fsync", fail_every_directory_fsync)
    rejected = json.loads(pr._handle_process({
        "action": "transfer", "session_id": process.id,
        "external_id": "persistent-fsync", "owner": "validator",
        "on_failure": "retry",
    }, task_id="agent-session"))
    assert "error" in rejected
    assert "durably compensate" in rejected["error"]
    assert process.external_run_owned is True
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = conn.execute(
            "SELECT id, status, outcome FROM task_runs WHERE owner_kind='external'"
        ).fetchone()
        assert task is not None and task.status == "running"
        assert run is not None and run["status"] == "running"
        assert run["outcome"] is None
        assert task.current_run_id == run["id"]
    registry.kill_process(process.id, source="test_cleanup")


def test_process_transfer_hands_a_claimed_worker_to_external_lifecycle_once(
    kanban_home, tmp_path, monkeypatch,
):
    """A real worker process can hand off its live claimed run before agent close."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="worker validation", assignee="default")
        claimed = kb.claim_task(conn, task_id, claimer="host:worker")
        assert claimed is not None and claimed.current_run_id is not None

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "host:worker")
    process = registry.spawn_local("sleep 0.4", task_id="agent-session")

    transfer = json.loads(pr._handle_process({
        "action": "transfer", "session_id": process.id, "external_id": "validation-1",
        "owner": "validator", "phase": "validation", "current": 1, "total": 2,
        "log_ref": "https://ci.example/validation-1", "result_ref": "artifact:pending",
        "max_retries": 2, "on_success": "resume", "on_failure": "retry",
    }, task_id="agent-session"))
    assert transfer["status"] == "transferred"
    assert process.settlement_pending is True
    external_run_id = transfer["run_id"]

    _bare_agent("agent-session").close()
    assert process.exited is False
    with kb.connect() as conn:
        task = kb.get_task(conn, task_id)
        run = kb.get_external_run(conn, external_run_id)
        agent_run = conn.execute("SELECT status, outcome FROM task_runs WHERE id=?", (claimed.current_run_id,)).fetchone()
        assert task is not None and task.current_run_id == external_run_id
        assert agent_run is not None and (agent_run["status"], agent_run["outcome"]) == ("external_handoff", "external_handoff")
        assert task.claim_lock == "external:validation-1"
        assert run is not None and run.phase == "validation"
        assert (run.progress_current, run.progress_total) == (1, 2)
        persisted_handoff = conn.execute(
            "SELECT managed_process_session_id, durable_result_path FROM task_runs WHERE id=?",
            (external_run_id,),
        ).fetchone()
        assert dict(persisted_handoff) == {
            "managed_process_session_id": process.id,
            "durable_result_path": str(kanban_home / "process-results" / f"{process.id}.json"),
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='protocol_violation'", (task_id,)
        ).fetchone()[0] == 0

    process._completion_event.wait(5)
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "ready"
        assert kb.get_external_run(conn, external_run_id).outcome == "completed"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='external_resumed'", (task_id,)
        ).fetchone()[0] == 1


def test_recovered_transferred_process_with_unknown_exit_is_lost_and_retried(
    kanban_home, tmp_path, monkeypatch,
):
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="unknown exit", assignee="operator", owner_kind="external",
        )
        run = kb.start_external_run(
            conn, task_id, owner="operator", external_id="unknown-exit", max_retries=2,
            on_failure="retry",
        )
    session = pr.ProcessSession(
        id="proc-recovered", command="unknown", kanban_external_run_id=run.id,
        kanban_board="default", kanban_external_owner="operator",
        external_run_owned=True, exited=True, exit_code=None, detached=True,
    )
    registry._running[session.id] = session
    registry._move_to_finished(session)
    with kb.connect() as conn:
        settled = kb.get_external_run(conn, run.id)
        assert settled is not None and settled.outcome == "lost"
        assert kb.get_task(conn, task_id).status == "ready"


def test_external_process_completion_reconciles_once(kanban_home, tmp_path, monkeypatch):
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="complete external", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="job-2")
    session = registry.spawn_local("true", task_id="agent-session")
    assert registry.transfer_to_external_run(session.id, task_id=task_id, run_id=run.id, owner="operator")
    session._completion_event.wait(5)

    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "done"
        assert registry.reconcile_external_completion(conn, session.id, owner="operator") is False
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == 1
        assert kb.get_task(conn, task_id).status == "done"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == 1


def test_transferred_process_completion_uses_current_durable_external_run_owner(kanban_home, tmp_path, monkeypatch):
    """A transfer to another external lane cannot strand an exited managed child."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="transfer owner", assignee="owner-a", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="owner-a", external_id="owner-transfer")
        assert kb.transfer_external_run_owner(conn, run.id, from_owner="owner-a", to_owner="owner-b")
    session = pr.ProcessSession(id="proc-owner-transfer", command="true", task_id="agent-session")
    registry._running[session.id] = session
    assert registry.transfer_to_external_run(
        session.id, task_id=task_id, run_id=run.id, owner="owner-a",
    )
    session.exited = True
    session.exit_code = 0
    registry._move_to_finished(session)
    assert session.kanban_external_owner == "owner-b"
    with kb.connect() as conn:
        settled = kb.get_external_run(conn, run.id)
        assert settled is not None and (settled.owner, settled.status, settled.outcome) == (
            "owner-b", "done", "completed",
        )
        assert kb.get_task(conn, task_id).status == "done"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)
        ).fetchone()[0] == 1


def test_transferred_process_settles_only_its_persisted_board(kanban_home, tmp_path, monkeypatch):
    """Same run ids on separate boards cannot make completion cross-board."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    kb.create_board("process-owner-a")
    kb.create_board("process-owner-b")
    with kb.connect(board="process-owner-a") as board_a, kb.connect(board="process-owner-b") as board_b:
        task_a = kb.create_task(board_a, title="board a", assignee="owner-a", owner_kind="external")
        run_a = kb.start_external_run(board_a, task_a, owner="owner-a", external_id="a-1")
        task_b = kb.create_task(board_b, title="board b", assignee="owner-b", owner_kind="external")
        run_b = kb.start_external_run(board_b, task_b, owner="owner-b", external_id="b-1")
        assert run_a.id == run_b.id  # Each isolated board starts its own run sequence.

    session = pr.ProcessSession(id="proc-board-a", command="true", task_id="agent-session")
    registry._running[session.id] = session
    assert registry.transfer_to_external_run(
        session.id, task_id=task_a, run_id=run_a.id, board="process-owner-a", owner="owner-a",
    )
    session.exited = True
    session.exit_code = 0
    registry._move_to_finished(session)

    with kb.connect(board="process-owner-a") as board_a, kb.connect(board="process-owner-b") as board_b:
        assert kb.get_task(board_a, task_a).status == "done"
        assert kb.get_external_run(board_a, run_a.id).status == "done"
        assert kb.get_task(board_b, task_b).status == "running"
        assert kb.get_external_run(board_b, run_b.id).status == "running"


def test_checkpoint_recovered_transferred_process_retains_owner_board_and_exact_run(
    kanban_home, tmp_path, monkeypatch,
):
    """A recovered live process settles its persisted board/run, not ambient state."""
    registry = pr.ProcessRegistry()
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    kb.create_board("checkpoint-owner-a")
    kb.create_board("checkpoint-owner-b")
    with kb.connect(board="checkpoint-owner-a") as board_a, kb.connect(board="checkpoint-owner-b") as board_b:
        task_a = kb.create_task(board_a, title="recover a", assignee="owner-a", owner_kind="external")
        run_a = kb.start_external_run(board_a, task_a, owner="owner-a", external_id="recover-a")
        task_b = kb.create_task(board_b, title="recover b", assignee="owner-b", owner_kind="external")
        run_b = kb.start_external_run(board_b, task_b, owner="owner-b", external_id="recover-b")
        assert run_a.id == run_b.id

    live = registry.spawn_local("sleep 30", task_id="agent-session")
    assert registry.transfer_to_external_run(
        live.id, task_id=task_a, run_id=run_a.id, board="checkpoint-owner-a", owner="owner-a",
    )
    recovered_registry = pr.ProcessRegistry()
    monkeypatch.setattr(recovered_registry, "_host_pid_is_ours", lambda pid, started: pid == live.pid)
    assert recovered_registry.recover_from_checkpoint() == 1
    recovered = recovered_registry._running[live.id]
    assert (recovered.kanban_board, recovered.kanban_external_owner) == ("checkpoint-owner-a", "owner-a")
    assert recovered.kanban_external_run_id == run_a.id

    # A reconstructed process has no Popen handle; its detached kill path must
    # still settle the durable run selected by the persisted handoff fields.
    assert recovered_registry.kill_process(live.id, source="test_cleanup")["status"] == "killed"
    with kb.connect(board="checkpoint-owner-a") as board_a, kb.connect(board="checkpoint-owner-b") as board_b:
        assert kb.get_external_run(board_a, run_a.id).outcome == "failed"
        assert kb.get_task(board_a, task_a).status == "blocked"
        assert kb.get_external_run(board_b, run_b.id).status == "running"
        assert kb.get_task(board_b, task_b).status == "running"


def test_ordinary_process_never_settles_a_kanban_external_run(kanban_home, tmp_path, monkeypatch):
    """Only an explicit handoff gives ProcessRegistry Kanban settlement authority."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="must remain running", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="unrelated")

    ordinary = pr.ProcessSession(id="proc-ordinary", command="true", task_id="agent-session")
    registry._running[ordinary.id] = ordinary
    ordinary.exited = True
    ordinary.exit_code = 0
    registry._move_to_finished(ordinary)

    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).status == "running"
        assert kb.get_task(conn, task_id).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == 0


def test_automatic_and_manual_external_reconciliation_race_emits_one_terminal_event(
    kanban_home, tmp_path, monkeypatch,
):
    """The ProcessRegistry and reconciler share one CAS terminal transition."""
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="race", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="race-1")

    session = pr.ProcessSession(id="proc-race", command="true", task_id="agent-session")
    registry._running[session.id] = session
    assert registry.transfer_to_external_run(session.id, task_id=task_id, run_id=run.id, owner="operator")
    session.exited = True
    session.exit_code = 0

    real_finish = kb.finish_external_run
    both_callers = threading.Barrier(2)
    finish_results = []

    def synchronized_finish(*args, **kwargs):
        both_callers.wait(timeout=5)
        result = real_finish(*args, **kwargs)
        finish_results.append(result)
        return result

    monkeypatch.setattr(kb, "finish_external_run", synchronized_finish)
    manual_result = []

    def manually_reconcile():
        with kb.connect() as conn:
            manual_result.append(registry.reconcile_external_completion(conn, session.id, owner="operator"))

    manual = threading.Thread(target=manually_reconcile)
    manual.start()
    registry._move_to_finished(session)
    manual.join(timeout=5)
    assert not manual.is_alive()

    with kb.connect() as conn:
        assert sorted(finish_results) == [False, True]
        assert len(manual_result) == 1
        assert kb.get_task(conn, task_id).status == "done"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == 1


def test_rejected_external_settlement_of_active_run_stays_checkpointed_and_retries(kanban_home, tmp_path, monkeypatch):
    """A false CAS result is terminal only when durable readback says it is."""
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    registry = pr.ProcessRegistry()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="retry rejected settlement", assignee="owner-a", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="owner-a", external_id="retry-rejected")
        assert kb.transfer_external_run_owner(conn, run.id, from_owner="owner-a", to_owner="owner-b")
    session = pr.ProcessSession(
        id="proc-rejected-settlement", command="true", kanban_task_id=task_id,
        kanban_external_run_id=run.id, kanban_board="default",
        kanban_external_owner="owner-a", external_run_owned=True,
        exited=True, exit_code=0, pid_scope="host", pid=None,
    )
    registry._running[session.id] = session
    real_finish = kb.finish_external_run
    monkeypatch.setattr(kb, "finish_external_run", lambda *_args, **_kwargs: False)
    registry._move_to_finished(session)
    assert session.id in registry._running
    assert session.id not in registry._finished
    entry = next(row for row in json.loads(checkpoint.read_text()) if row["session_id"] == session.id)
    assert entry["settlement_pending"] is True
    assert entry["kanban_external_owner"] == "owner-b"
    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).ended_at is None

    monkeypatch.setattr(kb, "finish_external_run", real_finish)
    restarted = pr.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 0
    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).outcome == "completed"
        assert kb.get_task(conn, task_id).status == "done"


def test_failed_external_settlement_stays_checkpointed_and_recovers_on_restart(
    kanban_home, tmp_path, monkeypatch,
):
    """A transient DB failure cannot erase the sole external-run settlement record."""
    checkpoint = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    registry = pr.ProcessRegistry()
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="recover settlement", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="recover-settlement")
    session = pr.ProcessSession(
        id="proc-settlement", command="true", kanban_task_id=task_id,
        kanban_external_run_id=run.id, kanban_board="default",
        kanban_external_owner="operator", external_run_owned=True,
        exited=True, exit_code=0, pid_scope="host", pid=None,
    )
    registry._running[session.id] = session
    real_connect_closing = kb.connect_closing
    monkeypatch.setattr(kb, "connect_closing", lambda **_kwargs: (_ for _ in ()).throw(OSError("db unavailable")))
    registry._move_to_finished(session)
    assert session.id in registry._running
    assert session.id not in registry._finished
    entry = next(row for row in json.loads(checkpoint.read_text()) if row["session_id"] == session.id)
    assert entry["settlement_pending"] is True

    monkeypatch.setattr(kb, "connect_closing", real_connect_closing)
    deadline = time.monotonic() + 5
    while session.id in registry._running and time.monotonic() < deadline:
        time.sleep(.05)
    assert session.id in registry._finished
    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).outcome == "completed"
        assert kb.get_task(conn, task_id).status == "done"
    assert not checkpoint.exists() or not json.loads(checkpoint.read_text())


@pytest.mark.parametrize(("exit_code", "outcome", "task_status"), [
    (0, "completed", "done"), (7, "failed", "blocked"),
])
def test_recovery_settles_transferred_helper_process_from_durable_sidecar(
    kanban_home, tmp_path, monkeypatch, exit_code, outcome, task_status,
):
    """A new registry learns the child result after its spawning worker is gone."""
    checkpoint = kanban_home / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="sidecar", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id=f"sidecar-{exit_code}")
    helper = "\n".join((
        "import json, sys",
        "from tools.process_registry import ProcessRegistry",
        "registry = ProcessRegistry()",
        "session = registry.spawn_local(sys.argv[1], task_id='helper-session')",
        "assert registry.transfer_to_external_run(session.id, task_id=sys.argv[2], run_id=int(sys.argv[3]), owner='operator')",
        "print(json.dumps({'session_id': session.id, 'result_path': session.durable_result_path}))",
    ))
    child = f"{sys.executable} -c \"import sys,time; time.sleep(.35); sys.exit({exit_code})\""
    result = __import__("subprocess").run(
        [sys.executable, "-c", helper, child, task_id, str(run.id)],
        cwd=str(Path(__file__).parents[2]), env={**os.environ, "HERMES_HOME": str(kanban_home)},
        text=True, capture_output=True, check=True,
    )
    handoff = json.loads(result.stdout)
    assert Path(handoff["result_path"]).parent == kanban_home / "process-results"
    time.sleep(.7)  # Helper is already gone; only the detached wrapper can write this.
    payload = json.loads(Path(handoff["result_path"]).read_text(encoding="utf-8"))
    assert payload == {"session_id": handoff["session_id"], "exit_code": exit_code}

    restarted = pr.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 0
    with kb.connect() as conn:
        settled = kb.get_external_run(conn, run.id)
        assert settled is not None and settled.outcome == outcome
        assert kb.get_task(conn, task_id).status == task_status
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'", (task_id,)).fetchone()[0] == (1 if exit_code == 0 else 0)
    assert not checkpoint.exists() or not json.loads(checkpoint.read_text())


def test_live_recovered_transfer_reads_durable_result_when_pid_later_exits(
    kanban_home, monkeypatch,
):
    """A process recovered while live must use its sidecar after later exit."""
    checkpoint = kanban_home / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="live sidecar", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="live-sidecar")
    helper = "\n".join((
        "import json, sys",
        "from tools.process_registry import ProcessRegistry",
        "registry = ProcessRegistry()",
        "session = registry.spawn_local(sys.argv[1], task_id='helper-session')",
        "assert registry.transfer_to_external_run(session.id, task_id=sys.argv[2], run_id=int(sys.argv[3]), owner='operator')",
        "print(json.dumps({'session_id': session.id}))",
    ))
    child = f"{sys.executable} -c \"import time; time.sleep(1.0)\""
    result = __import__("subprocess").run(
        [sys.executable, "-c", helper, child, task_id, str(run.id)],
        cwd=str(Path(__file__).parents[2]), env={**os.environ, "HERMES_HOME": str(kanban_home)},
        text=True, capture_output=True, check=True,
    )
    session_id = json.loads(result.stdout)["session_id"]

    restarted = pr.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 1
    assert restarted.get(session_id) is not None
    time.sleep(1.3)
    restarted.list_sessions()

    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).outcome == "completed"
        assert kb.get_task(conn, task_id).status == "done"
    assert not checkpoint.exists() or not json.loads(checkpoint.read_text())


@pytest.mark.parametrize("contents", [None, "not-json", '{"session_id":"wrong","exit_code":"0"}'])
def test_recovery_marks_transferred_process_lost_when_durable_sidecar_is_missing_or_corrupt(
    kanban_home, monkeypatch, contents,
):
    """A dead transferred PID without a valid exact sidecar follows the lost policy."""
    checkpoint = kanban_home / "processes.json"
    result_path = kanban_home / "process-results" / "proc-negative.json"
    if contents is not None:
        result_path.parent.mkdir()
        result_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="negative", assignee="operator", owner_kind="external")
        run = kb.start_external_run(conn, task_id, owner="operator", external_id="negative")
    checkpoint.write_text(json.dumps([{
        "session_id": "proc-negative", "command": "redacted", "pid": 99999999,
        "pid_scope": "host", "host_start_time": 1, "task_id": "helper-session",
        "kanban_task_id": task_id, "kanban_external_run_id": run.id,
        "kanban_board": "default", "kanban_external_owner": "operator",
        "external_run_owned": True, "durable_result_path": str(result_path),
    }]), encoding="utf-8")
    restarted = pr.ProcessRegistry()
    assert restarted.recover_from_checkpoint() == 0
    with kb.connect() as conn:
        assert kb.get_external_run(conn, run.id).outcome == "lost"
        assert kb.get_task(conn, task_id).status == "blocked"


def test_db_handoff_receipt_settles_after_crash_before_registry_checkpoint(
    kanban_home, monkeypatch,
):
    """The DB row alone recovers the exact sidecar when handoff crashes pre-checkpoint."""
    checkpoint = kanban_home / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="db-only handoff", assignee="operator")
        claimed = kb.claim_task(conn, task_id, claimer="worker")
        assert claimed is not None and claimed.current_run_id is not None
    helper = "\n".join((
        "import json, os, sys",
        "from tools.process_registry import ProcessRegistry",
        "from hermes_cli import kanban_db as kb",
        "registry = ProcessRegistry()",
        "session = registry.spawn_local(sys.argv[1], task_id='worker-session')",
        "with kb.connect() as conn:",
        " run = kb.handoff_agent_run_to_external(conn, sys.argv[2], expected_run_id=int(sys.argv[3]), expected_claim_lock='worker', owner='operator', external_id='db-only', pid=session.pid, managed_process_session_id=session.id, durable_result_path=session.durable_result_path, host_start_time=session.host_start_time, on_success='resume')",
        " assert run is not None",
        "print(json.dumps({'session_id': session.id, 'receipt': session.durable_result_path}))",
    ))
    child = f"{sys.executable} -c \"import sys,time; time.sleep(.25); sys.exit(0)\""
    result = __import__("subprocess").run(
        [sys.executable, "-c", helper, child, task_id, str(claimed.current_run_id)],
        cwd=str(Path(__file__).parents[2]), env={**os.environ, "HERMES_HOME": str(kanban_home)},
        text=True, capture_output=True, check=True,
    )
    handoff = json.loads(result.stdout)
    checkpoint.unlink(missing_ok=True)
    time.sleep(.6)
    assert Path(handoff["receipt"]).is_file()
    with kb.connect() as conn:
        conn.execute("UPDATE task_runs SET last_heartbeat_at=0 WHERE owner_kind='external'")
        assert kb.reconcile_stale_external_runs(conn, stale_after_seconds=1) == 1
        run = conn.execute("SELECT * FROM task_runs WHERE owner_kind='external'").fetchone()
        assert run["managed_process_session_id"] == handoff["session_id"]
        assert run["outcome"] == "completed"
        assert kb.get_task(conn, task_id).status == "ready"
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='external_resumed'",
            (task_id,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='completed'",
            (task_id,),
        ).fetchone()[0] == 0
