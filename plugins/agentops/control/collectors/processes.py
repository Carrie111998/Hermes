"""Read-only process snapshots using psutil, with command data fingerprinted."""

from __future__ import annotations

import hashlib
import os
import threading
import time
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

    def __init__(
        self,
        *,
        name_contains: str = "hermes",
        process_iter: Callable[[], Iterable[Any]] | None = None,
        max_items: int = 256,
        min_interval_seconds: float = 0.0,
    ) -> None:
        if not isinstance(name_contains, str) or max_items <= 0 or min_interval_seconds < 0:
            raise ValueError("invalid process matcher")
        self.name_contains = name_contains.casefold()
        self._process_iter = process_iter or (lambda: psutil.process_iter())
        self.max_items = max_items
        self.min_interval_seconds = min_interval_seconds
        self.source_id = "sha256:" + hashlib.sha256(f"process:{self.name_contains}".encode()).hexdigest()
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()

    @staticmethod
    def _command_fingerprint(process: Any) -> str:
        try:
            command = process.cmdline()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError):
            command = []
        digest = hashlib.sha256("\x00".join(str(part) for part in command).encode("utf-8")).hexdigest()
        return "sha256:" + digest

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        labels = target.spec.labels
        service_label = labels.get("service_label")
        profile_marker = labels.get("process_marker")
        expected_fingerprint = labels.get("command_fingerprint")
        if not service_label:
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        if not profile_marker or not expected_fingerprint:
            return failed_batch(target, self.name, "process_binding_unconfigured", source_id=self.source_id)
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
            self._last_collection = now
        observed_at = utc_now()
        signals = []
        inspected = 0
        try:
            processes = self._process_iter()
            for process in processes:
                inspected += 1
                if inspected > self.max_items:
                    break
                try:
                    name = str(process.name())
                    if self.name_contains and self.name_contains not in name.casefold():
                        continue
                    command = [str(part) for part in process.cmdline()]
                    fingerprint = self._command_fingerprint(process)
                    if profile_marker not in command and f"--profile={profile_marker}" not in command:
                        continue
                    if service_label not in command:
                        continue
                    owner = process.uids().real
                    if int(owner) != os.getuid() or fingerprint != expected_fingerprint:
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
                                    "command_fingerprint": fingerprint,
                                    "profile_marker": profile_marker,
                                    "owner_uid": int(owner),
                                },
                            )
                        )
                    )
                except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, OSError, ValueError):
                    continue
        except Exception:
            return failed_batch(target, self.name, "process_observation_failed", source_id=self.source_id)
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=tuple(signals),
            health=CollectorHealth(healthy=True),
            source_id=self.source_id,
        )
