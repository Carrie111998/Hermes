"""Read-only process snapshots using psutil, with command data fingerprinted."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

import psutil

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


class ProcessCollector:
    name = "processes"

    def __init__(self, *, name_contains: str = "hermes", process_iter: Callable[[], Iterable[Any]] | None = None) -> None:
        if not isinstance(name_contains, str):
            raise ValueError("invalid process matcher")
        self.name_contains = name_contains.casefold()
        self._process_iter = process_iter or (lambda: psutil.process_iter())

    @staticmethod
    def _command_fingerprint(process: Any) -> str:
        try:
            command = process.cmdline()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            command = []
        digest = hashlib.sha256("\x00".join(str(part) for part in command).encode("utf-8")).hexdigest()
        return "sha256:" + digest

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        observed_at = utc_now()
        signals = []
        try:
            processes = self._process_iter()
            for process in processes:
                try:
                    name = str(process.name())
                    if self.name_contains and self.name_contains not in name.casefold():
                        continue
                    signals.append(
                        redact_signal(
                            RawSignal(
                                target_id=target.target_id,
                                collector=self.name,
                                signal_type="process.snapshot",
                                observed_at=observed_at,
                                payload={
                                    "pid": int(process.pid),
                                    "name": name,
                                    "command_fingerprint": self._command_fingerprint(process),
                                },
                            )
                        )
                    )
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError, ValueError):
                    continue
        except Exception:
            return failed_batch(target, self.name, "process_observation_failed")
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=tuple(signals),
            health=CollectorHealth(healthy=True),
        )
