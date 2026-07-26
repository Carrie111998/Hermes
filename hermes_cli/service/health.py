"""Bounded health checks for manifest-declared services."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from hermes_cli.service.manifest import ServiceSpec


@dataclass(frozen=True)
class HealthResult:
    """One final health-check outcome after bounded retries."""

    healthy: bool
    outcome: str
    output: str
    error_repr: str | None = None
    attempts: int = 1


def pid_alive(pid: int | None) -> bool:
    """Return whether a PID exists and is not a zombie."""
    if pid is None or int(pid) <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        state = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "stat="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(state) and not state.startswith("Z")


def read_pid(service: ServiceSpec) -> int | None:
    """Read a positive integer PID from a service's declared file."""
    try:
        value = int(service.pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


def _check_once(
    service: ServiceSpec,
    *,
    pid: int | None,
    http_get: Callable[..., object],
    subprocess_run: Callable[..., subprocess.CompletedProcess],
) -> HealthResult:
    check = service.health_check
    kind = str(check["type"])
    timeout = float(check.get("timeout_seconds", 5))
    try:
        if kind == "pid_alive":
            candidate = pid if pid is not None else read_pid(service)
            alive = pid_alive(candidate)
            return HealthResult(
                healthy=alive,
                outcome="success" if alive else "failed",
                output=f"pid {candidate}: {'alive' if alive else 'not alive'}",
            )
        if kind == "http":
            response = http_get(str(check["url"]), timeout=timeout)
            status = int(getattr(response, "status_code"))
            expected = int(check.get("expected_status", 200))
            healthy = status == expected
            return HealthResult(
                healthy=healthy,
                outcome="success" if healthy else "failed",
                output=f"HTTP {status}; expected {expected}",
            )
        completed = subprocess_run(
            list(check["command"]),
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
        )
        stdout = str(completed.stdout or "")
        stderr = str(completed.stderr or "")
        regex = check.get("expected_stdout_regex")
        matched = (
            True
            if not regex
            else re.search(str(regex), stdout, re.MULTILINE | re.DOTALL)
            is not None
        )
        healthy = int(completed.returncode) == 0 and matched
        output = (
            f"exit={completed.returncode}; stdout={stdout[:2000]!r}; "
            f"stderr={stderr[:1000]!r}"
        )
        return HealthResult(
            healthy=healthy,
            outcome="success" if healthy else "failed",
            output=output,
        )
    except (httpx.TimeoutException, subprocess.TimeoutExpired) as exc:
        return HealthResult(
            healthy=False,
            outcome="timeout",
            output="health check timed out",
            error_repr=repr(exc)[:1000],
        )
    except Exception as exc:
        return HealthResult(
            healthy=False,
            outcome="failed",
            output="health check raised",
            error_repr=repr(exc)[:1000],
        )


def check_health(
    service: ServiceSpec,
    *,
    pid: int | None = None,
    attempts: int = 3,
    retry_delay_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    http_get: Callable[..., object] = httpx.get,
    subprocess_run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> HealthResult:
    """Run one of the supported checks with a fixed retry budget."""
    normalized_attempts = max(1, int(attempts))
    saw_timeout = False
    last = HealthResult(False, "failed", "health check did not run")
    for attempt in range(1, normalized_attempts + 1):
        last = _check_once(
            service,
            pid=pid,
            http_get=http_get,
            subprocess_run=subprocess_run,
        )
        saw_timeout = saw_timeout or last.outcome == "timeout"
        if last.healthy:
            return HealthResult(
                True,
                "success",
                last.output,
                last.error_repr,
                attempt,
            )
        if attempt < normalized_attempts:
            sleep(max(0.0, float(retry_delay_seconds)))
    return HealthResult(
        False,
        "timeout" if saw_timeout else "failed",
        last.output,
        last.error_repr,
        normalized_attempts,
    )


__all__ = ["HealthResult", "check_health", "pid_alive", "read_pid"]
