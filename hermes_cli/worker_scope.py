"""Independent transient scope construction for dispatcher-spawned workers."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from itertools import count
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

_PROBE_SEQUENCE = count(1)


def _unit_fragment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", value)[:48] or "unknown"


def _transient_scope_works(systemd_run: str) -> bool:
    unit = f"hermes-kanban-isolation-probe-{os.getpid()}-{next(_PROBE_SEQUENCE)}"
    try:
        probe = subprocess.run(
            [
                systemd_run,
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                "--property=KillMode=control-group",
                "--property=OOMPolicy=stop",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; print(Path('/proc/self/cgroup').read_text(), end='')",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if probe.returncode != 0:
        return False
    expected = f"/{unit}.scope"
    paths = [line.rsplit(":", 1)[-1].strip() for line in probe.stdout.splitlines() if ":" in line]
    return any(
        expected in path
        and "hermes-gateway" not in path.lower()
        and "dispatcher" not in path.lower()
        for path in paths
    )


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
    try:
        probe = subprocess.run(
            [systemctl, "--user", "is-system-running"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        if require_isolation:
            raise WorkerIsolationError("systemd user manager/bus is not healthy") from None
        return list(command)
    state = probe.stdout.strip()
    operational_state = (state == "running" and probe.returncode == 0) or (
        state == "degraded" and probe.returncode in {0, 1}
    )
    healthy_state = operational_state and _transient_scope_works(systemd_run)
    if not healthy_state:
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
