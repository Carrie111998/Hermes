from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from hermes_cli.programme import init as programme_init
from hermes_cli.service import cli as service_cli
from hermes_cli.service import schema as service_schema
from hermes_cli.service.health import pid_alive
from hermes_cli.service.runner import RestartRunner
from hermes_cli.sqlite_util import retrying_write_txn


DUMMY = Path(__file__).parent / "fixtures" / "dummy_service.py"


def _service(tmp_path: Path, *, crash: bool = False) -> dict:
    pid_file = tmp_path / "cli-service.pid"
    command = [
        sys.executable,
        str(DUMMY),
        "--pid-file",
        str(pid_file),
    ]
    if crash:
        command.append("--crash")
    return {
        "id": "cli_service",
        "name": "CLI Service",
        "pid_file": str(pid_file),
        "command": command,
        "working_dir": str(tmp_path),
        "env": {},
        "health_check": {"type": "pid_alive", "timeout_seconds": 0.2},
        "drain_timeout_seconds": 0.08,
        "start_timeout_seconds": 0.5,
        "depends_on": [],
        "tags": ["critical"],
    }


def _write_manifest(tmp_path: Path, *, crash: bool = False) -> Path:
    path = tmp_path / "services.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "services": [_service(tmp_path, crash=crash)],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _init_db(db_path: Path, state: str = "PAUSED") -> None:
    programme_init.migrate(db_path)
    conn = programme_init.connect(db_path)
    try:
        with retrying_write_txn(conn):
            conn.execute(
                """
                UPDATE programme_state
                   SET state = ?, reason = 'test', changed_by = 'test',
                       changed_at = '2026-07-25T00:00:00Z',
                       task_count_at_change = 0
                 WHERE id = 1
                """,
                (state,),
            )
    finally:
        conn.close()
    service_schema.ensure_migrated(db_path)


def _args(
    manifest: Path,
    db_path: Path,
    action: str,
    *,
    allow_active: bool = False,
    reason: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan=action == "plan",
        verify=action == "verify",
        status=action == "status",
        execute=action == "execute",
        dry_run_execute=action == "dry_run_execute",
        allow_active=allow_active,
        reason=reason,
        manifest=str(manifest),
        db_path=str(db_path),
    )


def _fast_runner(db_path: Path) -> RestartRunner:
    return RestartRunner(
        db_path=db_path,
        alert_sender=lambda _message: None,
        poll_interval_seconds=0.01,
        settle_seconds=0.05,
        health_retry_delay_seconds=0.01,
    )


def _patch_fast_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service_cli,
        "RestartRunner",
        lambda *, db_path: _fast_runner(Path(db_path)),
    )


def _cleanup_from_db(db_path: Path) -> None:
    conn = service_schema.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT new_pid FROM service_restart_log "
            "WHERE phase = 'start' AND new_pid IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        pid = int(row["new_pid"])
        if pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass


def _start_external(service: dict) -> subprocess.Popen:
    process = subprocess.Popen(
        service["command"],
        cwd=service["working_dir"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 2
    pid_file = Path(service["pid_file"])
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_alive(process.pid):
            return process
        time.sleep(0.01)
    raise AssertionError("external dummy did not start")


def test_cli_plan_prints_topological_order_no_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    assert service_cli._cmd_restart(
        _args(manifest, db_path, "plan")
    ) == 0
    output = capsys.readouterr().out
    conn = service_schema.connect(db_path)
    try:
        runs = conn.execute(
            "SELECT COUNT(*) AS count FROM service_restart_run"
        ).fetchone()["count"]
        states = conn.execute(
            "SELECT COUNT(*) AS count FROM service_manifest_state"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert "cli_service" in output
    assert "no health checks, signals, or process launches" in output
    assert runs == 0
    assert states == 1


def test_cli_verify_runs_health_checks_no_processes_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    service = yaml.safe_load(manifest.read_text())["services"][0]
    old = _start_external(service)
    _patch_fast_runner(monkeypatch)
    try:
        result = service_cli._cmd_restart(
            _args(manifest, db_path, "verify")
        )
        assert result == 0
        assert pid_alive(old.pid)
        assert int(Path(service["pid_file"]).read_text()) == old.pid
    finally:
        old.terminate()
        old.wait(timeout=2)


def test_cli_status_reads_latest_restart_per_service(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    conn = service_schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            run_id = conn.execute(
                """
                INSERT INTO service_restart_run (
                    started_at, ended_at, initiated_by, reason,
                    programme_state_at_start, overall_outcome
                ) VALUES ('a', 'b', 'test', NULL, 'PAUSED', 'success')
                """
            ).lastrowid
            conn.execute(
                """
                INSERT INTO service_restart_log (
                    run_id, service_id, phase, phase_started_at,
                    phase_ended_at, outcome, new_pid
                ) VALUES (?, 'alpha', 'health_check', 'a', 'b', 'success', 42)
                """,
                (run_id,),
            )
    finally:
        conn.close()
    result = service_cli._cmd_restart(
        _args(tmp_path / "unused.yaml", db_path, "status")
    )
    assert result == 0
    assert "alpha  health_check  success  42" in capsys.readouterr().out


def test_cli_execute_refuses_when_programme_running_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path, state="RUNNING")
    manifest = _write_manifest(tmp_path)
    _patch_fast_runner(monkeypatch)
    result = service_cli._cmd_restart(
        _args(manifest, db_path, "execute")
    )
    assert result == service_cli.EXIT_PROGRAMME_REFUSED
    allowed = service_cli._cmd_restart(
        _args(
            manifest,
            db_path,
            "execute",
            allow_active=True,
        )
    )
    try:
        assert allowed == service_cli.EXIT_SUCCESS
        assert "WARNING: programme is RUNNING" in capsys.readouterr().out
    finally:
        _cleanup_from_db(db_path)


def test_cli_execute_proceeds_when_programme_paused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    _patch_fast_runner(monkeypatch)
    result = service_cli._cmd_restart(
        _args(manifest, db_path, "execute")
    )
    try:
        assert result == 0
    finally:
        _cleanup_from_db(db_path)


def test_cli_execute_writes_run_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    _patch_fast_runner(monkeypatch)
    service_cli._cmd_restart(_args(manifest, db_path, "execute"))
    try:
        conn = service_schema.connect(db_path)
        try:
            row = conn.execute(
                "SELECT overall_outcome FROM service_restart_run"
            ).fetchone()
        finally:
            conn.close()
        assert row["overall_outcome"] == "success"
    finally:
        _cleanup_from_db(db_path)


def test_cli_dry_run_execute_does_not_spawn_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    _patch_fast_runner(monkeypatch)
    result = service_cli._cmd_restart(
        _args(manifest, db_path, "dry_run_execute")
    )
    conn = service_schema.connect(db_path)
    try:
        runs = conn.execute(
            "SELECT COUNT(*) AS count FROM service_restart_run"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert result == service_cli.EXIT_OPERATION_FAILED
    assert runs == 0
    assert not (tmp_path / "cli-service.pid").exists()


def test_cli_execute_records_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _write_manifest(tmp_path)
    _patch_fast_runner(monkeypatch)
    service_cli._cmd_restart(
        _args(
            manifest,
            db_path,
            "execute",
            reason="cs12 test reason",
        )
    )
    try:
        conn = service_schema.connect(db_path)
        try:
            reason = conn.execute(
                "SELECT reason FROM service_restart_run"
            ).fetchone()["reason"]
        finally:
            conn.close()
        assert reason == "cs12 test reason"
    finally:
        _cleanup_from_db(db_path)


def test_cli_help_lists_all_subcommands() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    service_cli.register_cli(subparsers)
    restart = next(
        action.choices["restart"]
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    help_text = restart.format_help()
    for flag in (
        "--plan",
        "--verify",
        "--status",
        "--execute",
        "--dry-run-execute",
    ):
        assert flag in help_text


def test_cli_exit_codes_documented() -> None:
    assert service_cli.EXIT_SUCCESS == 0
    assert service_cli.EXIT_OPERATION_FAILED == 1
    assert service_cli.EXIT_PROGRAMME_REFUSED == 3
    assert service_cli.EXIT_STRUCTURAL_ERROR == 4
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    service_cli.register_cli(subparsers)
    assert "3 programme-state refusal" in parser._actions[-1].choices[
        "restart"
    ].format_help()
