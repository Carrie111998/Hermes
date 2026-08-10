"""Optional bounded vault synchronization adapters."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class SyncResult:
    success: bool
    attempted: bool
    error: str = ""


class NoopSyncAdapter:
    def sync(self, reason: str) -> SyncResult:
        return SyncResult(True, False)

    def flush(self) -> SyncResult:
        return SyncResult(True, False)


class CommandSyncAdapter:
    def __init__(self, command: Sequence[str], debounce_seconds: float = 30.0, timeout: float = 60.0):
        self.command = tuple(command)
        self.debounce_seconds = debounce_seconds
        self.timeout = timeout
        self.dirty = False
        self._reasons: list[str] = []

    def mark_dirty(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)
        self.dirty = True

    def sync(self, reason: str) -> SyncResult:
        self.mark_dirty(reason)
        return self.flush()

    def flush(self) -> SyncResult:
        if not self.dirty:
            return SyncResult(True, False)
        try:
            completed = subprocess.run(
                list(self.command),
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SyncResult(False, True, type(exc).__name__)
        if completed.returncode != 0:
            return SyncResult(False, True, "command failed")
        self.dirty = False
        self._reasons.clear()
        return SyncResult(True, True)
