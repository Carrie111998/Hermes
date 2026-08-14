"""Safe, opt-in recovery for an existing Windows/WSL Claude session.

This module deliberately operates on an operator-configured existing Windows
Scheduled Task. It never creates a session, handles OAuth, reads credentials,
injects a TTY, or kills a process. The default configuration is disabled and
unconfigured so the known KLIB launcher cannot be silently reused for Hermes.
"""

from __future__ import annotations

import json
import math
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_CONFIG = {
    "enabled": False,
    "task_name": "",
    "remote_name": "",
    "wsl_distro": "Ubuntu",
    "claude_bin": "claude",
    "probe_timeout_seconds": 10,
    "repair_wait_seconds": 0,
}

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9_. -]+$")


@dataclass(frozen=True)
class RecoveryResult:
    """Redacted result safe for CLI output, logs, and cron delivery."""

    status: str
    detail: str
    task_state: str | None = None
    remote_count: int | None = None
    auth_logged_in: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "task_state": self.task_state,
            "remote_count": self.remote_count,
            "auth_logged_in": self.auth_logged_in,
        }


def _config(config: dict[str, Any] | None) -> dict[str, Any]:
    external_cli = (config or {}).get("external_cli", {})
    raw = (
        external_cli.get("remote_control_recovery", {})
        if isinstance(external_cli, dict)
        else {}
    )
    merged = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        merged.update(raw)
    return merged


def _validate_config(cfg: dict[str, Any]) -> str | None:
    if not isinstance(cfg.get("enabled"), bool):
        return "enabled must be a boolean"
    if not isinstance(cfg.get("task_name"), str) or not cfg["task_name"]:
        return "task_name is not configured"
    if not _SAFE_TASK_RE.fullmatch(cfg["task_name"]):
        return "task_name contains unsupported characters"
    if not isinstance(cfg.get("remote_name"), str) or not cfg["remote_name"]:
        return "remote_name is not configured"
    if not _SAFE_NAME_RE.fullmatch(cfg["remote_name"]):
        return "remote_name contains unsupported characters"
    if not isinstance(cfg.get("wsl_distro"), str) or not _SAFE_NAME_RE.fullmatch(
        cfg["wsl_distro"]
    ):
        return "wsl_distro contains unsupported characters"
    if not isinstance(cfg.get("claude_bin"), str) or not cfg["claude_bin"]:
        return "claude_bin is not configured"
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", cfg["claude_bin"]):
        return "claude_bin must be one executable name or path"
    for key in ("probe_timeout_seconds", "repair_wait_seconds"):
        value = cfg.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return f"{key} must be a non-negative number"
    return None


def _quote_ps(value: str) -> str:
    """Quote a value for a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def _runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(1.0, float(timeout)),
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def _powershell(cfg: dict[str, Any], operation: str) -> subprocess.CompletedProcess[str] | None:
    binary = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not binary:
        return None
    task = _quote_ps(cfg["task_name"])
    if operation == "state":
        script = (
            f"$t=Get-ScheduledTask -TaskName {task} -ErrorAction Stop; "
            "[Console]::WriteLine([string]$t.State)"
        )
    elif operation == "start":
        script = f"Start-ScheduledTask -TaskName {task} -ErrorAction Stop"
    else:  # pragma: no cover - private helper contract
        raise ValueError(f"unsupported PowerShell operation: {operation}")
    return _runner(
        [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        cfg["probe_timeout_seconds"],
    )


def _wsl(cfg: dict[str, Any], args: list[str]) -> subprocess.CompletedProcess[str] | None:
    binary = shutil.which("wsl.exe") or shutil.which("wsl")
    if not binary:
        return None
    return _runner(
        [binary, "-d", cfg["wsl_distro"], "--", *args],
        cfg["probe_timeout_seconds"],
    )


def _task_state(cfg: dict[str, Any]) -> tuple[str | None, str | None]:
    result = _powershell(cfg, "state")
    if result is None:
        return None, "powershell.exe or task query unavailable"
    if result.returncode != 0:
        return None, "scheduled task not found or inaccessible"
    state = result.stdout.strip()
    if state not in {"Ready", "Running", "Disabled", "Queued"}:
        return None, "scheduled task returned an unknown state"
    return state, None


def _remote_count(cfg: dict[str, Any]) -> tuple[int | None, str | None]:
    marker = f"claude remote-control --name {cfg['remote_name']}"
    result = _wsl(cfg, ["pgrep", "-fc", "--", marker])
    if result is None:
        return None, "wsl process probe unavailable"
    output = result.stdout.strip()
    if output.isdigit():
        return int(output), None
    if result.returncode == 1 and not output:
        return 0, None
    return None, "wsl process probe failed"


def _auth_state(cfg: dict[str, Any]) -> tuple[bool | None, str | None]:
    result = _wsl(cfg, [cfg["claude_bin"], "auth", "status"])
    if result is None:
        return None, "claude auth probe unavailable"
    text = (result.stdout + "\n" + result.stderr).lower()
    if re.search(r"logged\s*in\s*[:=]\s*false", text) or "not logged in" in text:
        return False, None
    if re.search(r"logged\s*in\s*[:=]\s*true", text) or "logged in" in text:
        return True, None
    return None, "claude auth result was not recognized"


def inspect(config: dict[str, Any] | None = None) -> RecoveryResult:
    """Inspect the configured session without starting or stopping anything."""
    cfg = _config(config)
    if platform.system() != "Windows":
        return RecoveryResult(
            "UNSUPPORTED_HOST",
            "requires the native Windows host that owns Task Scheduler",
        )
    if not cfg.get("enabled", False):
        return RecoveryResult("DISABLED", "remote control recovery is disabled")
    error = _validate_config(cfg)
    if error:
        return RecoveryResult("NOT_CONFIGURED", error)

    task_state, task_error = _task_state(cfg)
    if task_error:
        return RecoveryResult("TASK_UNAVAILABLE", task_error, task_state=task_state)
    auth_logged_in, auth_error = _auth_state(cfg)
    if auth_error:
        return RecoveryResult("AUTH_CHECK_FAILED", auth_error, task_state=task_state)
    if auth_logged_in is False:
        return RecoveryResult(
            "REAUTH_REQUIRED", "Claude CLI is not authenticated", task_state=task_state,
            auth_logged_in=False,
        )
    count, count_error = _remote_count(cfg)
    if count_error:
        return RecoveryResult(
            "PROCESS_CHECK_FAILED", count_error, task_state=task_state,
            auth_logged_in=auth_logged_in,
        )
    if count > 1:
        status = "AMBIGUOUS_MULTIPLE_SESSIONS"
        detail = "more than one matching Remote Control session"
    elif count == 1:
        status = "READY"
        detail = "one configured Remote Control session is present"
    elif task_state != "Ready":
        status = f"TASK_{task_state.upper()}_REMOTE_CONTROL_MISSING"
        detail = f"task is {task_state.lower()} but the configured session is not present"
    else:
        status = "REMOTE_CONTROL_MISSING"
        detail = "configured Remote Control session is not present"
    return RecoveryResult(status, detail, task_state, count, auth_logged_in)


def repair(config: dict[str, Any] | None = None) -> RecoveryResult:
    """Start only the configured existing task when repair is safe."""
    before = inspect(config)
    if before.status != "REMOTE_CONTROL_MISSING":
        return before
    cfg = _config(config)
    started = _powershell(cfg, "start")
    if started is None or started.returncode != 0:
        return RecoveryResult(
            "REPAIR_FAILED", "existing scheduled task could not be started",
            before.task_state, before.remote_count, before.auth_logged_in,
        )
    wait_seconds = max(0, min(int(cfg.get("repair_wait_seconds", 0)), 30))
    deadline = time.monotonic() + wait_seconds
    transient_statuses = {
        "REMOTE_CONTROL_MISSING",
        "TASK_RUNNING_REMOTE_CONTROL_MISSING",
        "TASK_QUEUED_REMOTE_CONTROL_MISSING",
    }
    while wait_seconds and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0, deadline - time.monotonic())))
        after = inspect(config)
        if after.status == "READY":
            return after
        if after.status not in transient_statuses:
            return after
    return RecoveryResult(
        "REPAIR_TRIGGERED",
        "existing scheduled task accepted; readiness requires a later status check",
        before.task_state,
        before.remote_count,
        before.auth_logged_in,
    )


def claude_recovery_command(args) -> int:
    """CLI handler; JSON is optional and remains redacted."""
    from hermes_cli.config import load_config

    result = repair(load_config()) if args.action == "repair" else inspect(load_config())
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True))
    else:
        print(f"{result.status}: {result.detail}")
        if result.task_state:
            print(f"task_state={result.task_state}")
        if result.remote_count is not None:
            print(f"remote_count={result.remote_count}")
        if result.auth_logged_in is not None:
            print(f"auth_logged_in={str(result.auth_logged_in).lower()}")
    return 0 if result.status in {"READY", "ALREADY_READY", "REPAIR_TRIGGERED", "DISABLED", "NOT_CONFIGURED"} else 1
