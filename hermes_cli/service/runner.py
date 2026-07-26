"""Synthetic-testable coordinated service restart runner."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from hermes_cli.programme.gate import get_state
from hermes_cli.service import schema
from hermes_cli.service.health import (
    HealthResult,
    check_health,
    pid_alive,
    read_pid,
)
from hermes_cli.service.manifest import (
    Manifest,
    ServiceSpec,
    compute_restart_order,
    merge_service_env,
)
from hermes_cli.sqlite_util import retrying_write_txn


class ProgrammeRefused(RuntimeError):
    """Raised before a restart run when programme state is unsafe."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


@dataclass(frozen=True)
class RunResult:
    """Final audited outcome for a restart invocation."""

    run_id: int
    overall_outcome: str
    programme_state_at_start: str
    service_order: tuple[str, ...]


@dataclass(frozen=True)
class _PhaseResult:
    outcome: str
    output: str = ""
    error_repr: str | None = None
    pid: int | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _truncate(value: object, limit: int = 4_000) -> str:
    return str(value or "")[:limit]


class RestartRunner:
    """Execute one ordered restart while persisting every phase."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        initiated_by: str = "operator",
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        health_checker: Callable[..., HealthResult] = check_health,
        alert_sender: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        poll_interval_seconds: float = 0.25,
        settle_seconds: float = 1.0,
        health_retry_delay_seconds: float = 2.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.initiated_by = str(initiated_by)
        self.popen_factory = popen_factory
        self.health_checker = health_checker
        self.alert_sender = alert_sender
        self.sleep = sleep
        self.poll_interval_seconds = max(
            0.01,
            float(poll_interval_seconds),
        )
        self.settle_seconds = max(0.0, float(settle_seconds))
        self.health_retry_delay_seconds = max(
            0.0,
            float(health_retry_delay_seconds),
        )

    def _write(self, operation):
        schema.ensure_migrated(self.db_path)
        conn = schema.connect(self.db_path)
        try:
            with retrying_write_txn(conn):
                return operation(conn)
        finally:
            conn.close()

    def _create_run(
        self,
        *,
        programme_state: str,
        reason: str | None,
    ) -> int:
        def _insert(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO service_restart_run (
                    started_at, initiated_by, reason,
                    programme_state_at_start, overall_outcome
                ) VALUES (?, ?, ?, ?, 'in_progress')
                """,
                (
                    _utc_now(),
                    self.initiated_by,
                    reason,
                    programme_state,
                ),
            )
            return int(cursor.lastrowid)

        return int(self._write(_insert))

    def _finish_run(self, run_id: int, outcome: str) -> None:
        def _update(conn: sqlite3.Connection) -> None:
            cursor = conn.execute(
                """
                UPDATE service_restart_run
                   SET ended_at = ?, overall_outcome = ?
                 WHERE id = ?
                   AND overall_outcome = 'in_progress'
                """,
                (_utc_now(), outcome, int(run_id)),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"restart run {run_id} is not in progress")

        self._write(_update)

    def _log_phase(
        self,
        *,
        run_id: int,
        service_id: str,
        phase: str,
        result: _PhaseResult,
        started_at: str,
        old_pid: int | None = None,
        new_pid: int | None = None,
    ) -> None:
        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO service_restart_log (
                    run_id, service_id, phase, phase_started_at,
                    phase_ended_at, outcome, old_pid, new_pid,
                    health_check_output, error_repr
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id),
                    service_id,
                    phase,
                    started_at,
                    _utc_now(),
                    result.outcome,
                    old_pid,
                    new_pid,
                    _truncate(result.output),
                    _truncate(result.error_repr, 1_000)
                    if result.error_repr
                    else None,
                ),
            )

        self._write(_insert)

    def _wait_dead(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return True
            self.sleep(self.poll_interval_seconds)
        return not pid_alive(pid)

    @staticmethod
    def _live_command(pid: int) -> str | None:
        try:
            completed = subprocess.run(
                ["ps", "-p", str(int(pid)), "-o", "command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        value = str(completed.stdout or "").strip()
        return value or None

    def _command_matches(self, service: ServiceSpec, pid: int) -> bool:
        live = self._live_command(pid)
        if live is None:
            return False
        executable = Path(service.command[0]).name
        if executable not in live:
            return False
        if len(service.command) > 1 and service.command[1].endswith(
            (".py", ".sh")
        ):
            return Path(service.command[1]).name in live
        return True

    def drain(self, service: ServiceSpec) -> _PhaseResult:
        """Terminate a declared PID, escalating only after its drain timeout."""
        pid = read_pid(service)
        if pid is None or not pid_alive(pid):
            return _PhaseResult(
                "success",
                "no_running_pid",
                pid=pid,
            )
        if not self._command_matches(service, pid):
            return _PhaseResult(
                "failed",
                "pid command does not match manifest",
                error_repr=(
                    f"refusing to signal pid {pid}: command identity mismatch"
                ),
                pid=pid,
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return _PhaseResult("success", "already_exited", pid=pid)
        except OSError as exc:
            return _PhaseResult(
                "failed",
                "SIGTERM failed",
                error_repr=repr(exc),
                pid=pid,
            )
        if self._wait_dead(pid, service.drain_timeout_seconds):
            return _PhaseResult("success", "clean_exit", pid=pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return _PhaseResult("success", "exited_before_sigkill", pid=pid)
        except OSError as exc:
            return _PhaseResult(
                "failed",
                "SIGKILL failed",
                error_repr=repr(exc),
                pid=pid,
            )
        self.sleep(min(0.5, self.poll_interval_seconds * 2))
        if pid_alive(pid):
            return _PhaseResult(
                "failed",
                "pid remained alive after SIGKILL",
                error_repr=f"pid {pid} survived escalation",
                pid=pid,
            )
        return _PhaseResult(
            "timeout",
            "SIGTERM timeout; SIGKILL escalation succeeded",
            pid=pid,
        )

    @staticmethod
    def stop(service: ServiceSpec) -> _PhaseResult:
        """Remove the stale PID file after the process is confirmed stopped."""
        try:
            service.pid_file.unlink(missing_ok=True)
        except OSError as exc:
            return _PhaseResult(
                "failed",
                "pid file cleanup failed",
                error_repr=repr(exc),
            )
        return _PhaseResult("success", "pid_file_removed")

    @staticmethod
    def _write_pid_file(path: Path, pid: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(f"{int(pid)}\n", encoding="utf-8")
        os.replace(temporary, path)

    def start(self, service: ServiceSpec) -> _PhaseResult:
        """Spawn one declared process and reject an immediate crash."""
        try:
            process = self.popen_factory(
                list(service.command),
                cwd=str(service.working_dir),
                env=merge_service_env(service),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            pid = int(process.pid)
            self._write_pid_file(service.pid_file, pid)
            self.sleep(min(self.settle_seconds, service.start_timeout_seconds))
            if not pid_alive(pid):
                return _PhaseResult(
                    "failed",
                    "process exited during startup settle",
                    error_repr=f"new pid {pid} is not alive",
                    pid=pid,
                )
            return _PhaseResult("success", "process_started", pid=pid)
        except Exception as exc:
            return _PhaseResult(
                "failed",
                "process launch failed",
                error_repr=repr(exc),
            )

    def verify_service(
        self,
        service: ServiceSpec,
        *,
        pid: int | None = None,
    ) -> HealthResult:
        """Run the declared bounded health policy."""
        return self.health_checker(
            service,
            pid=pid,
            attempts=3,
            retry_delay_seconds=self.health_retry_delay_seconds,
            sleep=self.sleep,
        )

    def verify(self, manifest: Manifest) -> list[tuple[ServiceSpec, HealthResult]]:
        """Read-only verification in computed restart order."""
        return [
            (service, self.verify_service(service))
            for service in compute_restart_order(manifest)
        ]

    def dry_run_execute(
        self,
        manifest: Manifest,
    ) -> list[tuple[ServiceSpec, HealthResult]]:
        """Exercise ordering and health only; never signal or spawn a process."""
        return self.verify(manifest)

    def _skip_remaining(
        self,
        *,
        run_id: int,
        services: list[ServiceSpec],
        old_pids: dict[str, int | None],
    ) -> None:
        for service in services:
            started_at = _utc_now()
            self._log_phase(
                run_id=run_id,
                service_id=service.id,
                phase="skipped",
                result=_PhaseResult(
                    "skipped",
                    "skipped after critical failure",
                ),
                started_at=started_at,
                old_pid=old_pids.get(service.id),
            )

    def _emit_alert(
        self,
        *,
        run_id: int,
        service: ServiceSpec,
        phase: str,
        programme_state: str,
        error_repr: str,
    ) -> None:
        from hermes_cli.cost import telegram_alert
        from hermes_cli.side_effects import api as side_effects

        message = (
            "⚠️ SERVICE RESTART FAILED\n"
            f"service: {service.name} ({service.id})\n"
            f"run_id: {run_id}\n"
            f"phase: {phase}\n"
            f"programme_state: {programme_state}\n"
            f"error: {_truncate(error_repr, 500)}"
        )
        reservation = side_effects.reserve(
            task_id=f"system:service_restart:{run_id}",
            lane="platform",
            action_type="telegram.send",
            payload={"target": "telegram", "message": message},
            idempotency_key=(
                f"service_restart_failure:{service.id}:{run_id}"
            ),
            db_path=self.db_path,
        )
        if (
            reservation.already_done is not None
            or reservation.already_in_flight is not None
            or reservation.reserved_id is None
        ):
            return
        row_id = int(reservation.reserved_id)
        side_effects.mark_in_flight(
            reserved_id=row_id,
            db_path=self.db_path,
        )
        sender = self.alert_sender or telegram_alert.send_bridge_alert
        try:
            sender(message)
        except Exception as exc:
            side_effects.fail(
                reserved_id=row_id,
                error_class=type(exc).__name__,
                error_message=str(exc),
                db_path=self.db_path,
            )
            return
        side_effects.confirm(
            reserved_id=row_id,
            external_ref=None,
            result_summary="service restart alert delivered",
            db_path=self.db_path,
        )

    def execute(
        self,
        manifest: Manifest,
        *,
        allow_active: bool = False,
        reason: str | None = None,
    ) -> RunResult:
        """Restart every service in order, stopping on a critical failure."""
        state = get_state(self.db_path, migrate_if_missing=False)
        programme_state = str(state.state)
        if programme_state == "RUNNING" and not allow_active:
            raise ProgrammeRefused(
                "Programme is RUNNING. Pause it with `hermes gate pause` "
                "before restart, or explicitly pass --allow-active.",
                exit_code=3,
            )
        if programme_state not in {
            "PAUSED",
            "ADMITTING_ONLY_HEALTH",
            "RUNNING",
        }:
            raise ProgrammeRefused(
                f"Unsupported programme state for restart: {programme_state}",
                exit_code=4,
            )
        run_reason = (
            "forced_while_running"
            if programme_state == "RUNNING" and allow_active
            else reason
        )
        run_id = self._create_run(
            programme_state=programme_state,
            reason=run_reason,
        )
        ordered = compute_restart_order(manifest)
        old_pids = {service.id: read_pid(service) for service in ordered}
        overall = "success"

        for index, service in enumerate(ordered):
            old_pid = old_pids[service.id]
            drain_started = _utc_now()
            drain = self.drain(service)
            self._log_phase(
                run_id=run_id,
                service_id=service.id,
                phase="drain",
                result=drain,
                started_at=drain_started,
                old_pid=old_pid,
            )
            if drain.outcome == "failed":
                overall = "failed"
                self._skip_remaining(
                    run_id=run_id,
                    services=ordered[index + 1 :],
                    old_pids=old_pids,
                )
                self._emit_alert(
                    run_id=run_id,
                    service=service,
                    phase="drain",
                    programme_state=programme_state,
                    error_repr=drain.error_repr or drain.output,
                )
                break

            stop_started = _utc_now()
            stopped = self.stop(service)
            self._log_phase(
                run_id=run_id,
                service_id=service.id,
                phase="stop",
                result=stopped,
                started_at=stop_started,
                old_pid=old_pid,
            )
            if stopped.outcome == "failed":
                overall = "failed"
                self._skip_remaining(
                    run_id=run_id,
                    services=ordered[index + 1 :],
                    old_pids=old_pids,
                )
                self._emit_alert(
                    run_id=run_id,
                    service=service,
                    phase="stop",
                    programme_state=programme_state,
                    error_repr=stopped.error_repr or stopped.output,
                )
                break

            start_started = _utc_now()
            started = self.start(service)
            new_pid = started.pid
            self._log_phase(
                run_id=run_id,
                service_id=service.id,
                phase="start",
                result=started,
                started_at=start_started,
                old_pid=old_pid,
                new_pid=new_pid,
            )
            if started.outcome == "failed":
                overall = "failed" if service.is_critical else "partial"
                self._emit_alert(
                    run_id=run_id,
                    service=service,
                    phase="start",
                    programme_state=programme_state,
                    error_repr=started.error_repr or started.output,
                )
                if service.is_critical:
                    self._skip_remaining(
                        run_id=run_id,
                        services=ordered[index + 1 :],
                        old_pids=old_pids,
                    )
                    break
                continue

            health_started = _utc_now()
            checked = self.verify_service(service, pid=new_pid)
            health_phase = _PhaseResult(
                checked.outcome,
                checked.output,
                checked.error_repr,
                new_pid,
            )
            self._log_phase(
                run_id=run_id,
                service_id=service.id,
                phase="health_check",
                result=health_phase,
                started_at=health_started,
                old_pid=old_pid,
                new_pid=new_pid,
            )
            if not checked.healthy:
                overall = "failed" if service.is_critical else "partial"
                self._emit_alert(
                    run_id=run_id,
                    service=service,
                    phase="health_check",
                    programme_state=programme_state,
                    error_repr=checked.error_repr or checked.output,
                )
                if service.is_critical:
                    self._skip_remaining(
                        run_id=run_id,
                        services=ordered[index + 1 :],
                        old_pids=old_pids,
                    )
                    break

        self._finish_run(run_id, overall)
        return RunResult(
            run_id=run_id,
            overall_outcome=overall,
            programme_state_at_start=programme_state,
            service_order=tuple(service.id for service in ordered),
        )


def latest_status(
    *,
    db_path: str | Path,
) -> list[dict[str, object]]:
    """Read the newest logged phase for each service."""
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT l.*
              FROM service_restart_log AS l
              JOIN (
                    SELECT service_id, MAX(id) AS max_id
                      FROM service_restart_log
                     GROUP BY service_id
              ) AS latest
                ON latest.max_id = l.id
             ORDER BY l.service_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


__all__ = [
    "ProgrammeRefused",
    "RestartRunner",
    "RunResult",
    "latest_status",
]
