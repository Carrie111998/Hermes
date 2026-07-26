from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from hermes_cli.programme import init as programme_init
from hermes_cli.service import schema as service_schema
from hermes_cli.service.health import check_health, pid_alive
from hermes_cli.service.manifest import validate_manifest
from hermes_cli.service.runner import (
    ProgrammeRefused,
    RestartRunner,
    _PhaseResult,
)


DUMMY = Path(__file__).parent / "fixtures" / "dummy_service.py"


def _service(
    tmp_path: Path,
    service_id: str = "alpha",
    *,
    depends_on: list[str] | None = None,
    tags: list[str] | None = None,
    crash: bool = False,
    ignore_term_seconds: float = 0.0,
    env: dict[str, str] | None = None,
    health_check: dict | None = None,
) -> dict:
    pid_file = tmp_path / f"{service_id}.pid"
    command = [
        sys.executable,
        str(DUMMY),
        "--pid-file",
        str(pid_file),
        "--ignore-term-seconds",
        str(ignore_term_seconds),
    ]
    if crash:
        command.append("--crash")
    return {
        "id": service_id,
        "name": service_id.title(),
        "pid_file": str(pid_file),
        "command": command,
        "working_dir": str(tmp_path),
        "env": env or {},
        "health_check": health_check
        or {"type": "pid_alive", "timeout_seconds": 0.2},
        "drain_timeout_seconds": 0.08,
        "start_timeout_seconds": 0.5,
        "depends_on": depends_on or [],
        "tags": tags or [],
    }


def _manifest(*services: dict):
    return validate_manifest(
        {"schema_version": 1, "services": list(services)}
    )


def _init_db(db_path: Path, state: str = "PAUSED") -> None:
    programme_init.migrate(db_path)
    conn = programme_init.connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
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
        conn.execute("COMMIT")
    finally:
        conn.close()
    service_schema.ensure_migrated(db_path)


def _runner(
    db_path: Path,
    *,
    alerts: list[str] | None = None,
) -> RestartRunner:
    return RestartRunner(
        db_path=db_path,
        alert_sender=(alerts.append if alerts is not None else lambda _: None),
        poll_interval_seconds=0.01,
        settle_seconds=0.05,
        health_retry_delay_seconds=0.01,
    )


def _wait_for_pid(path: Path, timeout: float = 2.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            time.sleep(0.01)
            continue
        if pid_alive(pid):
            return pid
        time.sleep(0.01)
    raise AssertionError(f"dummy service did not become ready: {path}")


def _start_external(service) -> subprocess.Popen:
    process = subprocess.Popen(
        list(service.command),
        cwd=str(service.working_dir),
        env={**os.environ, **service.env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    _wait_for_pid(service.pid_file)
    return process


def _cleanup_pid(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    if pid_alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, ProcessLookupError):
        pass


def _logged_new_pids(db_path: Path) -> list[int]:
    conn = service_schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT new_pid
              FROM service_restart_log
             WHERE phase = 'start' AND new_pid IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return [int(row["new_pid"]) for row in rows]


def test_drain_sigterm_clean_exit(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(_service(tmp_path)).services[0]
    old = _start_external(service)
    result = _runner(db_path).drain(service)
    old.wait(timeout=2)
    assert result.outcome == "success"
    assert not pid_alive(old.pid)


def test_drain_no_running_pid_success_note(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(_service(tmp_path)).services[0]
    result = _runner(db_path).drain(service)
    assert result.outcome == "success"
    assert result.output == "no_running_pid"


def test_drain_ignored_sigterm_escalates_to_sigkill(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(
        _service(tmp_path, ignore_term_seconds=60)
    ).services[0]
    old = _start_external(service)
    result = _runner(db_path).drain(service)
    old.wait(timeout=2)
    assert result.outcome == "timeout"
    assert "SIGKILL" in result.output


def test_drain_kill_failure_aborts_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _manifest(
        _service(tmp_path, "critical", tags=["critical"]),
        _service(tmp_path, "later"),
    )
    runner = _runner(db_path)
    monkeypatch.setattr(
        runner,
        "drain",
        lambda _service: _PhaseResult(
            "failed",
            "SIGTERM failed",
            "PermissionError",
        ),
    )
    result = runner.execute(manifest)
    assert result.overall_outcome == "failed"
    conn = service_schema.connect(db_path)
    try:
        skipped = conn.execute(
            "SELECT COUNT(*) AS count FROM service_restart_log "
            "WHERE phase = 'skipped'"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert skipped == 1


def test_drain_stale_pid_file_treated_as_no_running_pid(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(_service(tmp_path)).services[0]
    service.pid_file.write_text("999999\n", encoding="utf-8")
    result = _runner(db_path).drain(service)
    assert result.outcome == "success"
    assert result.output == "no_running_pid"


def test_start_launches_subprocess_and_writes_pid_file(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(_service(tmp_path)).services[0]
    result = _runner(db_path).start(service)
    try:
        assert result.outcome == "success"
        assert int(service.pid_file.read_text()) == result.pid
        assert pid_alive(result.pid)
    finally:
        _cleanup_pid(result.pid)


def test_start_records_new_pid_in_log(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    result = _runner(db_path).execute(
        _manifest(_service(tmp_path))
    )
    try:
        conn = service_schema.connect(db_path)
        try:
            row = conn.execute(
                "SELECT new_pid FROM service_restart_log "
                "WHERE phase = 'start'"
            ).fetchone()
        finally:
            conn.close()
        assert row["new_pid"] in _logged_new_pids(db_path)
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)
    assert result.overall_outcome == "success"


def test_start_instant_crash_marked_failed(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    service = _manifest(
        _service(tmp_path, crash=True)
    ).services[0]
    result = _runner(db_path).start(service)
    assert result.outcome == "failed"
    assert "startup" in result.output


def test_start_env_merge_applied(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    observed = tmp_path / "env.txt"
    raw = _service(tmp_path, env={"CS12_TEST_VALUE": "merged"})
    raw["command"] = [
        sys.executable,
        "-c",
        (
            "import os,time,pathlib;"
            f"pathlib.Path({str(observed)!r}).write_text("
            "os.environ['CS12_TEST_VALUE']);time.sleep(30)"
        ),
    ]
    service = _manifest(raw).services[0]
    result = _runner(db_path).start(service)
    try:
        assert result.outcome == "success"
        assert observed.read_text(encoding="utf-8") == "merged"
    finally:
        _cleanup_pid(result.pid)


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_health_check_http_success(tmp_path: Path) -> None:
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "http",
                "url": "http://example.test/health",
                "expected_status": 200,
                "timeout_seconds": 1,
            },
        )
    ).services[0]
    result = check_health(
        service,
        attempts=1,
        http_get=lambda *_args, **_kwargs: _Response(200),
    )
    assert result.healthy


def test_health_check_http_wrong_status_failed(tmp_path: Path) -> None:
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "http",
                "url": "http://example.test/health",
                "expected_status": 200,
                "timeout_seconds": 1,
            },
        )
    ).services[0]
    result = check_health(
        service,
        attempts=1,
        http_get=lambda *_args, **_kwargs: _Response(503),
    )
    assert not result.healthy
    assert result.outcome == "failed"


def test_health_check_http_timeout(tmp_path: Path) -> None:
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "http",
                "url": "http://example.test/health",
                "expected_status": 200,
                "timeout_seconds": 1,
            },
        )
    ).services[0]

    def _timeout(*_args, **_kwargs):
        raise httpx.ReadTimeout("slow")

    result = check_health(service, attempts=1, http_get=_timeout)
    assert result.outcome == "timeout"


def test_health_check_exec_regex_match(tmp_path: Path) -> None:
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "exec",
                "command": [
                    sys.executable,
                    "-c",
                    "print('first\\nsecond')",
                ],
                "expected_stdout_regex": "first.*second",
                "timeout_seconds": 1,
            },
        )
    ).services[0]
    assert check_health(service, attempts=1).healthy


def test_health_check_exec_regex_no_match_failed(tmp_path: Path) -> None:
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "exec",
                "command": [sys.executable, "-c", "print('nope')"],
                "expected_stdout_regex": "^expected$",
                "timeout_seconds": 1,
            },
        )
    ).services[0]
    assert not check_health(service, attempts=1).healthy


def test_health_check_pid_alive_success(tmp_path: Path) -> None:
    service = _manifest(_service(tmp_path)).services[0]
    result = check_health(service, pid=os.getpid(), attempts=1)
    assert result.healthy


def test_health_check_retries_three_times_before_failed(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    service = _manifest(
        _service(
            tmp_path,
            health_check={
                "type": "http",
                "url": "http://example.test/health",
                "expected_status": 200,
                "timeout_seconds": 1,
            },
        )
    ).services[0]

    def _unhealthy(*_args, **_kwargs):
        calls.append(1)
        return _Response(503)

    result = check_health(
        service,
        attempts=3,
        retry_delay_seconds=0,
        http_get=_unhealthy,
    )
    assert not result.healthy
    assert len(calls) == 3


def test_run_success_all_services_healthy(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _manifest(
        _service(tmp_path, "critical", tags=["critical"]),
        _service(tmp_path, "worker_a", depends_on=["critical"]),
        _service(tmp_path, "worker_b", depends_on=["critical"]),
    )
    old_processes = [_start_external(service) for service in manifest.services]
    old_pids = {service.id: process.pid for service, process in zip(
        manifest.services,
        old_processes,
        strict=True,
    )}
    result = _runner(db_path).execute(
        manifest,
        reason="cs12 scenario",
    )
    try:
        for process in old_processes:
            process.wait(timeout=2)
        new_pids = {
            service.id: int(service.pid_file.read_text())
            for service in manifest.services
        }
        assert all(new_pids[key] != old_pids[key] for key in old_pids)
        assert result.overall_outcome == "success"
        conn = service_schema.connect(db_path)
        try:
            run = conn.execute(
                "SELECT overall_outcome FROM service_restart_run"
            ).fetchone()
            phases = conn.execute(
                "SELECT COUNT(*) AS count FROM service_restart_log"
            ).fetchone()["count"]
        finally:
            conn.close()
        assert run["overall_outcome"] == "success"
        assert phases == 12
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)


def test_run_records_run_row_and_per_phase_log_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    result = _runner(db_path).execute(
        _manifest(_service(tmp_path))
    )
    try:
        conn = service_schema.connect(db_path)
        try:
            run_count = conn.execute(
                "SELECT COUNT(*) AS count FROM service_restart_run"
            ).fetchone()["count"]
            phases = [
                row["phase"]
                for row in conn.execute(
                    "SELECT phase FROM service_restart_log ORDER BY id"
                )
            ]
        finally:
            conn.close()
        assert run_count == 1
        assert phases == ["drain", "stop", "start", "health_check"]
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)
    assert result.overall_outcome == "success"


def test_run_critical_failure_skips_subsequent_services(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    alerts: list[str] = []
    manifest = _manifest(
        _service(
            tmp_path,
            "critical",
            tags=["critical"],
            crash=True,
        ),
        _service(tmp_path, "later", depends_on=["critical"]),
    )
    result = _runner(db_path, alerts=alerts).execute(manifest)
    conn = service_schema.connect(db_path)
    try:
        skipped = conn.execute(
            "SELECT service_id FROM service_restart_log "
            "WHERE phase = 'skipped'"
        ).fetchone()
        bucket = conn.execute(
            "SELECT status FROM side_effects "
            "WHERE action_type = 'telegram.send'"
        ).fetchone()
    finally:
        conn.close()
    assert result.overall_outcome == "failed"
    assert skipped["service_id"] == "later"
    assert bucket["status"] == "done"
    assert len(alerts) == 1


def test_run_non_critical_failure_continues_marks_partial(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _manifest(
        _service(tmp_path, "broken", crash=True),
        _service(tmp_path, "healthy"),
    )
    result = _runner(db_path).execute(manifest)
    try:
        conn = service_schema.connect(db_path)
        try:
            healthy_started = conn.execute(
                """
                SELECT COUNT(*) AS count
                  FROM service_restart_log
                 WHERE service_id = 'healthy' AND phase = 'start'
                """
            ).fetchone()["count"]
        finally:
            conn.close()
        assert healthy_started == 1
        assert result.overall_outcome == "partial"
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)


def test_run_respects_topological_order(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    manifest = _manifest(
        _service(tmp_path, "child", depends_on=["parent"]),
        _service(tmp_path, "parent"),
    )
    _runner(db_path).execute(manifest)
    try:
        conn = service_schema.connect(db_path)
        try:
            order = [
                row["service_id"]
                for row in conn.execute(
                    "SELECT service_id FROM service_restart_log "
                    "WHERE phase = 'start' ORDER BY id"
                )
            ]
        finally:
            conn.close()
        assert order == ["parent", "child"]
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)


def test_run_refuses_when_programme_state_RUNNING_without_flag(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path, state="RUNNING")
    with pytest.raises(ProgrammeRefused) as caught:
        _runner(db_path).execute(_manifest(_service(tmp_path)))
    assert caught.value.exit_code == 3
    conn = service_schema.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM service_restart_run"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert count == 0


def test_run_proceeds_when_programme_state_RUNNING_with_allow_active(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path, state="RUNNING")
    result = _runner(db_path).execute(
        _manifest(_service(tmp_path)),
        allow_active=True,
        reason="ignored",
    )
    try:
        conn = service_schema.connect(db_path)
        try:
            row = conn.execute(
                "SELECT reason FROM service_restart_run"
            ).fetchone()
        finally:
            conn.close()
        assert result.overall_outcome == "success"
        assert row["reason"] == "forced_while_running"
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)


def test_run_never_modifies_programme_state(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    conn = programme_init.connect(db_path)
    try:
        before = tuple(
            conn.execute("SELECT * FROM programme_state").fetchone()
        )
    finally:
        conn.close()
    _runner(db_path).execute(_manifest(_service(tmp_path)))
    try:
        conn = programme_init.connect(db_path)
        try:
            after = tuple(
                conn.execute("SELECT * FROM programme_state").fetchone()
            )
        finally:
            conn.close()
        assert before == after
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)


def test_run_telegram_alert_bucketed_via_side_effects(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    alerts: list[str] = []
    manifest = _manifest(
        _service(
            tmp_path,
            "critical",
            tags=["critical"],
            crash=True,
        )
    )
    result = _runner(db_path, alerts=alerts).execute(manifest)
    conn = service_schema.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT task_id, status, action_type FROM side_effects"
        ).fetchall()
    finally:
        conn.close()
    assert result.overall_outcome == "failed"
    assert len(rows) == 1
    assert rows[0]["action_type"] == "telegram.send"
    assert rows[0]["status"] == "done"
    assert len(alerts) == 1


def test_run_no_telegram_alert_when_overall_success(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "kanban.db"
    _init_db(db_path)
    alerts: list[str] = []
    _runner(db_path, alerts=alerts).execute(
        _manifest(_service(tmp_path))
    )
    try:
        conn = service_schema.connect(db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'side_effects'"
            ).fetchone()
            count = (
                conn.execute(
                    "SELECT COUNT(*) AS count FROM side_effects"
                ).fetchone()["count"]
                if exists is not None
                else 0
            )
        finally:
            conn.close()
        assert count == 0
        assert alerts == []
    finally:
        for pid in _logged_new_pids(db_path):
            _cleanup_pid(pid)
