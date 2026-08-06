"""Local and SSH command execution for readiness scanners."""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Normalized command result independent of the execution backend."""

    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner(Protocol):
    """Execution contract used by readiness scanners."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult: ...


class LocalRunner:
    """Execute commands directly on the local host."""

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        normalized = tuple(str(part) for part in command)
        completed = subprocess.run(
            normalized,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(
            command=normalized,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class SSHRunner:
    """Execute commands through an existing OpenSSH host configuration."""

    def __init__(self, host: str, user: str | None = None) -> None:
        if not host.strip():
            raise ValueError("host must not be empty")
        self._target = f"{user}@{host}" if user else host

    def run(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> CommandResult:
        normalized = tuple(str(part) for part in command)
        remote_command = shlex.join(normalized)

        if cwd is not None:
            remote_command = (
                f"cd {shlex.quote(str(cwd))} && {remote_command}"
            )

        ssh_command = (
            "ssh",
            "-o",
            "BatchMode=yes",
            self._target,
            remote_command,
        )
        completed = subprocess.run(
            ssh_command,
            capture_output=True,
            text=True,
            check=False,
        )
        return CommandResult(
            command=normalized,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
