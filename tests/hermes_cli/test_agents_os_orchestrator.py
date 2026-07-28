from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from hermes_cli.agents_os import AgentsOSService, connect, resolve_paths, utc_now
from hermes_cli.agents_os_commands import confirm_command, create_command, get_command
from hermes_cli.agents_os_execution import ProbeResult, RuntimeAdapterRegistry, RuntimeInvocation
from hermes_cli.agents_os_orchestrator import ExecutionCoordinator, execution_projection
from hermes_cli import agents_os_web


class LocalProcessAdapter:
    name = "fake"

    def __init__(self, cwd: Path, code: str):
        self.cwd, self.code = cwd, code

    def probe(self):
        return ProbeResult(self.name, True, sys.executable)

    def build_invocation(self, *, prompt: str, cwd: Path, **_options):
        assert cwd == self.cwd
        return RuntimeInvocation(self.name, (sys.executable, "-c", self.code), cwd, prompt.encode(),
                                 (sys.executable, "-c", "<local-test-program>"))


def _prepared(tmp_path: Path, monkeypatch, code: str):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    paths = resolve_paths(None)
    with connect(paths) as conn:
        now = utc_now()
        conn.execute("INSERT INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,0)",
                     ("task-real", "Real proof", "ready", "code-task", 1, now, now, "test prompt", "runtime:fake"))
        command = create_command(conn, transcript="test prompt", idempotency_key=f"test-{code}",
                                 metadata={"task_id": "task-real", "workflow": "code-task", "profile_id": "doni"})
        command = confirm_command(conn, command["id"], expected_version=command["version"])
        conn.commit()
    registry = RuntimeAdapterRegistry([LocalProcessAdapter(tmp_path, code)])
    return paths, command, ExecutionCoordinator(paths, allowed_cwds=[tmp_path], registry=registry)


def _wait(paths, run_id: str):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with connect(paths) as conn:
            item = execution_projection(conn, run_id)
        if item and item["status"] not in {"queued", "running", "cancelling"}:
            return item
        time.sleep(0.02)
    raise AssertionError("execution did not finish")


def test_real_local_process_success_creates_evidence_and_scoped_memory(tmp_path, monkeypatch):
    paths, command, coordinator = _prepared(tmp_path, monkeypatch, "print('verified result')")
    queued = coordinator.queue(command_id=command["id"], runtime="fake", cwd=tmp_path,
                               approved_model_call=True, timeout_seconds=5)
    result = _wait(paths, queued["run_id"])
    assert result["status"] == "succeeded"
    assert result["exit_code"] == 0
    assert Path(result["stdout_path"]).read_text().strip() == "verified result"
    with connect(paths) as conn:
        run = conn.execute("SELECT status FROM runs WHERE id=?", (queued["run_id"],)).fetchone()
        memory = conn.execute("SELECT scope,task_id FROM memory_objects").fetchone()
        persisted = get_command(conn, command["id"])
    assert run[0] == "succeeded"
    assert tuple(memory) == ("task", "task-real")
    assert persisted["state"] == "succeeded"


def test_nonzero_process_is_never_success(tmp_path, monkeypatch):
    paths, command, coordinator = _prepared(tmp_path, monkeypatch, "import sys; print('bad'); sys.exit(7)")
    queued = coordinator.queue(command_id=command["id"], runtime="fake", cwd=tmp_path,
                               approved_model_call=True, timeout_seconds=5)
    result = _wait(paths, queued["run_id"])
    assert result["status"] == "failed"
    assert result["exit_code"] == 7
    with connect(paths) as conn:
        assert conn.execute("SELECT status FROM runs WHERE id=?", (queued["run_id"],)).fetchone()[0] == "failed"
        assert get_command(conn, command["id"])["state"] == "failed"


def test_operator_approval_is_required_before_process_start(tmp_path, monkeypatch):
    paths, command, coordinator = _prepared(tmp_path, monkeypatch, "print('must not run')")
    try:
        coordinator.queue(command_id=command["id"], runtime="fake", cwd=tmp_path,
                          approved_model_call=False, timeout_seconds=5)
    except PermissionError as exc:
        assert "explicit operator approval" in str(exc)
    else:
        raise AssertionError("model call started without explicit approval")
    with connect(paths) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runtime_executions").fetchone()[0] == 0


def test_jarvis_service_create_confirm_run_and_memory_search(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("AGENTS_OS_RUNTIME_CWDS", str(tmp_path))
    paths = resolve_paths(None)
    service = AgentsOSService(paths)
    registry = RuntimeAdapterRegistry([LocalProcessAdapter(tmp_path, "print('jarvis operational')")])
    coordinator = ExecutionCoordinator(paths, allowed_cwds=[tmp_path], registry=registry)
    agents_os_web._COORDINATORS[str(paths.db.resolve())] = coordinator
    created = agents_os_web.jarvis_create_command_action(
        service, {"transcript_text": "Show local status", "idempotency_key": "web-e2e"}
    )
    started = agents_os_web.jarvis_start_command_action(
        service, created["command"]["id"],
        {"expected_version": created["command"]["version"], "runtime": "fake",
         "cwd": str(tmp_path), "approved_model_call": True, "timeout_seconds": 5},
    )
    result = _wait(paths, started["execution"]["run_id"])
    assert result["status"] == "succeeded"
    found = agents_os_web.memory_search_action(
        paths, {"query": "jarvis", "profile_id": "doni", "scopes": ["task"],
                "task_id": created["command"]["metadata"]["task_id"]},
    )
    assert found["count"] == 1
    assert found["items"][0]["producer_runtime"] == "fake"


def test_jarvis_draft_cancel_is_durable_for_command_and_task(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    paths = resolve_paths(None)
    service = AgentsOSService(paths)
    created = agents_os_web.jarvis_create_command_action(
        service, {"transcript_text": "Show local status", "idempotency_key": "cancel-e2e"}
    )
    cancelled = agents_os_web.jarvis_cancel_command_action(
        service, created["command"]["id"], {"expected_version": 1, "reason": "test"}
    )
    assert cancelled["command"]["state"] == "cancelled"
    task_id = created["command"]["metadata"]["task_id"]
    with connect(paths) as conn:
        assert conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0] == "cancelled"
