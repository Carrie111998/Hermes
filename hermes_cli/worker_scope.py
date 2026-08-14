"""Independent transient scope construction for dispatcher-spawned workers."""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Mapping, Sequence


class WorkerIsolationError(RuntimeError):
    pass


PINNED_ENV = (
    "HERMES_PROFILE",
    "HERMES_HOME",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_BOARD",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_MODEL",
    "HERMES_KANBAN_PROVIDER",
    "HERMES_KANBAN_TOOLSETS",
    "TERMINAL_CWD",
)


def _unit_fragment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:48] or "unknown"


def build_scoped_worker_command(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    require_isolation: bool,
) -> list[str]:
    systemd_run = shutil.which("systemd-run")
    systemctl = shutil.which("systemctl")
    if not systemd_run or not systemctl:
        if require_isolation:
            raise WorkerIsolationError("systemd transient scope backend unavailable")
        return list(command)
    probe = subprocess.run(
        [systemctl, "--user", "is-system-running"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "running":
        if require_isolation:
            raise WorkerIsolationError("systemd user manager/bus is not healthy")
        return list(command)
    task = _unit_fragment(env.get("HERMES_KANBAN_TASK", "task"))
    run = _unit_fragment(env.get("HERMES_KANBAN_RUN_ID", "run"))
    wrapped = [
        systemd_run,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit=hermes-kanban-{task}-r{run}",
        "--property=KillMode=control-group",
        "--property=OOMPolicy=stop",
    ]
    for key in PINNED_ENV:
        if key in env:
            wrapped.append(f"--setenv={key}={env[key]}")
    wrapped.extend(["--", *command])
    return wrapped
