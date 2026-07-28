"""Execution coordinator joining commands, runtime adapters and shared memory."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from hermes_cli.agents_os import AgentsOSPaths, connect, log_event, utc_now
from hermes_cli.agents_os_commands import (
    acknowledge_cancel,
    cancel_command,
    complete_command,
    get_command,
    mark_running,
)
from hermes_cli.agents_os_execution import (
    CancelToken,
    RuntimeAdapterRegistry,
    default_registry,
    execute_invocation,
)
from hermes_cli.agents_os_memory import create_memory_candidate, create_memory_object


EXECUTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_executions (
    run_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued','running','succeeded','failed','timed_out','cancelling','cancelled'
    )),
    cwd TEXT NOT NULL,
    evidence_argv TEXT NOT NULL DEFAULT '[]',
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    stdout_path TEXT,
    stderr_path TEXT,
    result_path TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runtime_executions_status
    ON runtime_executions(status, created_at);
"""


def ensure_execution_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(EXECUTION_SCHEMA)


def resolve_allowed_cwds(paths: AgentsOSPaths) -> tuple[Path, ...]:
    """Return exact runtime roots; callers cannot expand this through requests."""
    configured = os.environ.get("AGENTS_OS_RUNTIME_CWDS", "").strip()
    raw = [item.strip() for item in configured.split(os.pathsep) if item.strip()]
    if not raw:
        raw = [str(paths.home)]
    resolved: list[Path] = []
    for item in raw:
        path = Path(item).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise ValueError(f"runtime cwd is not a directory: {path}")
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved)


def execution_projection(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    ensure_execution_schema(conn)
    old = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM runtime_executions WHERE run_id=?", (run_id,)).fetchone()
    finally:
        conn.row_factory = old
    if row is None:
        return None
    item = dict(row)
    try:
        item["evidence_argv"] = json.loads(item["evidence_argv"] or "[]")
    except json.JSONDecodeError:
        item["evidence_argv"] = []
    item["timed_out"] = bool(item["timed_out"])
    item["cancelled"] = bool(item["cancelled"])
    return item


class ExecutionCoordinator:
    """Own background process handles for one local Agents OS instance."""

    def __init__(
        self,
        paths: AgentsOSPaths,
        *,
        allowed_cwds: list[Path] | tuple[Path, ...],
        registry: RuntimeAdapterRegistry | None = None,
    ) -> None:
        self.paths = paths
        self.allowed_cwds = tuple(Path(item) for item in allowed_cwds)
        self.registry = registry or default_registry(allowed_cwds=self.allowed_cwds)
        self._lock = threading.RLock()
        self._tokens: dict[str, CancelToken] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._capabilities_cache: tuple[float, dict[str, Any]] | None = None

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            if self._capabilities_cache and time.monotonic() - self._capabilities_cache[0] < 60.0:
                return self._capabilities_cache[1]
        probes = self.registry.probe_all()
        payload = {
            name: {
                "runtime": probe.runtime,
                "available": probe.available,
                "executable": probe.executable,
                "reason": probe.reason,
            }
            for name, probe in probes.items()
        }
        with self._lock:
            self._capabilities_cache = (time.monotonic(), payload)
        return payload

    def queue(
        self,
        *,
        command_id: str,
        runtime: str,
        cwd: Path,
        approved_model_call: bool,
        timeout_seconds: float = 300.0,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not approved_model_call:
            raise PermissionError("explicit operator approval is required for a real model call")
        adapter = self.registry.get(runtime)
        probe = adapter.probe()
        if not probe.available:
            raise RuntimeError(probe.reason or f"runtime unavailable: {runtime}")
        with connect(self.paths) as conn:
            ensure_execution_schema(conn)
            command = get_command(conn, command_id)
            if command["state"] != "queued":
                raise ValueError(f"command must be queued, got {command['state']}")
            if command.get("run_id"):
                existing = execution_projection(conn, command["run_id"])
                if existing:
                    return existing
            run_id = f"run-{uuid.uuid4().hex[:12]}"
            workflow = str((command.get("metadata") or {}).get("workflow") or "jarvis-command")
            task_id = (command.get("metadata") or {}).get("task_id")
            now = utc_now()
            conn.execute(
                "INSERT INTO runs(id,task_id,workflow,status,input,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, task_id, workflow, "queued", command["transcript"], now),
            )
            conn.execute(
                "INSERT INTO runtime_executions(run_id,command_id,runtime,status,cwd,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, command_id, runtime, "queued", str(cwd), now),
            )
            log_event(conn, "execution_queued", task_id=task_id, run_id=run_id,
                      payload={"runtime": runtime, "command_id": command_id})
            conn.commit()
        token = CancelToken()
        thread = threading.Thread(
            target=self._run,
            kwargs={
                "run_id": run_id,
                "command_id": command_id,
                "runtime": runtime,
                "cwd": Path(cwd),
                "timeout_seconds": timeout_seconds,
                "options": dict(options or {}),
                "token": token,
            },
            daemon=True,
            name=f"agents-os-{run_id}",
        )
        with self._lock:
            self._tokens[run_id] = token
            self._threads[run_id] = thread
        thread.start()
        with connect(self.paths) as conn:
            return execution_projection(conn, run_id) or {"run_id": run_id, "status": "queued"}

    def _run(
        self,
        *,
        run_id: str,
        command_id: str,
        runtime: str,
        cwd: Path,
        timeout_seconds: float,
        options: dict[str, Any],
        token: CancelToken,
    ) -> None:
        task_id: str | None = None
        try:
            with connect(self.paths) as conn:
                command = get_command(conn, command_id)
                task_id = (command.get("metadata") or {}).get("task_id")
                command = mark_running(conn, command_id, expected_version=command["version"], run_id=run_id)
                conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
                conn.execute(
                    "UPDATE runtime_executions SET status='running',started_at=? WHERE run_id=?",
                    (utc_now(), run_id),
                )
                if task_id:
                    conn.execute("UPDATE tasks SET status='in_progress',updated_at=? WHERE id=? AND status IN ('ready','pending','routed')", (utc_now(), task_id))
                log_event(conn, "execution_started", task_id=task_id, run_id=run_id,
                          payload={"runtime": runtime, "command_id": command_id})
                conn.commit()

            run_dir = self.paths.artifacts / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            adapter = self.registry.get(runtime)
            if runtime == "codex":
                options.setdefault("output_last_message", run_dir / "last-message.txt")
            invocation = adapter.build_invocation(prompt=command["transcript"], cwd=cwd, **options)
            with connect(self.paths) as conn:
                conn.execute(
                    "UPDATE runtime_executions SET evidence_argv=? WHERE run_id=?",
                    (json.dumps(invocation.evidence_argv, ensure_ascii=False), run_id),
                )
                conn.commit()
            result = execute_invocation(invocation, timeout_seconds=timeout_seconds, cancel_token=token)
            stdout_path = run_dir / "stdout.log"
            stderr_path = run_dir / "stderr.log"
            result_path = run_dir / "result.txt"
            stdout_path.write_bytes(result.stdout)
            stderr_path.write_bytes(result.stderr)
            preferred = run_dir / "last-message.txt"
            result_text = preferred.read_text(encoding="utf-8", errors="replace") if preferred.exists() else result.stdout.decode("utf-8", errors="replace")
            result_path.write_text(result_text, encoding="utf-8")
            status = "cancelled" if result.cancelled else "timed_out" if result.timed_out else "succeeded" if result.succeeded else "failed"
            with connect(self.paths) as conn:
                now = utc_now()
                current = get_command(conn, command_id)
                if result.succeeded:
                    profile_id = str((command.get("metadata") or {}).get("profile_id") or "doni")
                    create_memory_object(
                        conn, kind="run_result", title=f"Result {run_id}", body_text=result_text,
                        body_uri=str(result_path), scope="task", profile_id=profile_id,
                        producer_runtime=runtime, producer_agent=runtime, task_id=task_id or command_id,
                        run_id=run_id, provenance={"workflow": (command.get("metadata") or {}).get("workflow"), "write_origin": "agents_os_execution"},
                    )
                    if task_id:
                        conn.execute("UPDATE tasks SET status='review',updated_at=? WHERE id=?", (now, task_id))
                else:
                    create_memory_candidate(
                        conn, result_text=result_text or f"{status}: see {stderr_path}", profile_id="doni",
                        producer_runtime=runtime, producer_agent=runtime, task_id=task_id, run_id=run_id,
                    )
                    if task_id:
                        conn.execute("UPDATE tasks SET status='blocked',updated_at=? WHERE id=? AND status='in_progress'", (now, task_id))

                # Command transitions also initialize their schema with
                # executescript(), which may commit an existing transaction.
                # Complete the command before publishing terminal run status;
                # runtime_executions is the readiness barrier used by pollers.
                if result.cancelled and current["state"] == "cancelling":
                    acknowledge_cancel(conn, command_id, expected_version=current["version"])
                elif current["state"] == "running":
                    complete_command(
                        conn, command_id, expected_version=current["version"], succeeded=result.succeeded,
                        result={"text": result_text, "run_id": run_id, "result_path": str(result_path)} if result.succeeded else None,
                        error=None if result.succeeded else {"status": status, "exit_code": result.returncode, "stderr_path": str(stderr_path)},
                    )

                conn.execute(
                    """UPDATE runtime_executions SET status=?,exit_code=?,timed_out=?,cancelled=?,
                       stdout_path=?,stderr_path=?,result_path=?,completed_at=? WHERE run_id=?""",
                    (status, result.returncode, int(result.timed_out), int(result.cancelled),
                     str(stdout_path), str(stderr_path), str(result_path), now, run_id),
                )
                mapped = "succeeded" if result.succeeded else "failed"
                conn.execute("UPDATE runs SET status=?,completed_at=? WHERE id=?", (mapped, now, run_id))
                log_event(conn, "execution_completed", task_id=task_id, run_id=run_id,
                          payload={"status": status, "exit_code": result.returncode, "result_path": str(result_path)})
                conn.commit()
        except Exception as exc:
            with connect(self.paths) as conn:
                ensure_execution_schema(conn)
                now = utc_now()
                conn.execute(
                    "UPDATE runtime_executions SET status='failed',error=?,completed_at=? WHERE run_id=?",
                    (f"{exc.__class__.__name__}: {exc}", now, run_id),
                )
                conn.execute("UPDATE runs SET status='failed',completed_at=? WHERE id=?", (now, run_id))
                try:
                    current = get_command(conn, command_id)
                    if current["state"] == "running":
                        complete_command(conn, command_id, expected_version=current["version"], succeeded=False,
                                         error={"type": exc.__class__.__name__, "message": str(exc)})
                except Exception:
                    pass
                log_event(conn, "execution_failed", task_id=task_id, run_id=run_id,
                          payload={"error_type": exc.__class__.__name__, "message": str(exc)})
                conn.commit()
        finally:
            with self._lock:
                self._tokens.pop(run_id, None)
                self._threads.pop(run_id, None)

    def cancel(self, *, run_id: str, command_id: str, expected_version: int, reason: str = "operator") -> dict[str, Any]:
        with connect(self.paths) as conn:
            command = cancel_command(conn, command_id, expected_version=expected_version, reason=reason)
            conn.execute("UPDATE runtime_executions SET status=? WHERE run_id=?", ("cancelling" if command["state"] == "cancelling" else "cancelled", run_id))
            if command["state"] == "cancelled":
                conn.execute("UPDATE runs SET status='failed',completed_at=? WHERE id=?", (utc_now(), run_id))
            conn.commit()
        with self._lock:
            token = self._tokens.get(run_id)
        if token is not None:
            token.cancel()
        return command
