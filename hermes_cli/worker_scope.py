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


def _manager_state_is_operational(stdout: str, returncode: int) -> bool:
    """Accept only an exact systemd state token and its documented result code."""
    state = stdout[:-1] if stdout.endswith("\n") else stdout
    if not state or "\n" in state or not state.isascii():
        return False
    return (state == "running" and returncode == 0) or (
        state == "degraded" and returncode in {0, 1}
    )


def _cgroup_output_matches_scope(stdout: str, unit: str) -> bool:
    """Validate complete /proc/self/cgroup output and exact probe placement."""
    if not stdout or not stdout.isascii():
        return False
    body = stdout[:-1] if stdout.endswith("\n") else stdout
    if not body or body.endswith("\n"):
        return False

    expected_component = f"{unit}.scope"
    hierarchy_ids: set[str] = set()
    for record in body.split("\n"):
        if not record or record.count(":") != 2:
            return False
        hierarchy_id, controllers, path = record.split(":")
        if not hierarchy_id.isascii() or not hierarchy_id.isdecimal():
            return False
        if len(hierarchy_id) > 1 and hierarchy_id.startswith("0"):
            return False
        if hierarchy_id in hierarchy_ids:
            return False
        hierarchy_ids.add(hierarchy_id)

        if hierarchy_id == "0":
            if controllers:
                return False
        else:
            controller_names = controllers.split(",")
            if not controllers or len(controller_names) != len(set(controller_names)):
                return False
            if any(
                not re.fullmatch(r"(?:name=)?[A-Za-z0-9_.-]+", name)
                for name in controller_names
            ):
                return False

        if not path.startswith("/"):
            return False
        components = path[1:].split("/")
        if not components or any(
            not component
            or component in {".", ".."}
            or any(not 0x21 <= ord(char) <= 0x7E for char in component)
            for component in components
        ):
            return False
        lowered = [component.lower() for component in components]
        if any("gateway" in component or "dispatcher" in component for component in lowered):
            return False
        if components.count(expected_component) != 1:
            return False
    return bool(hierarchy_ids)


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
    return _cgroup_output_matches_scope(probe.stdout, unit)


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
    operational_state = _manager_state_is_operational(probe.stdout, probe.returncode)
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
