"""Read plist configuration facts without changing service state."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
import threading
import time
from pathlib import Path

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    RawSignal,
    Target,
    asset_source_id,
    target_allows_asset,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


class LaunchdCollector:
    """Read the declared plist only; runtime control is intentionally absent."""

    name = "launchd"

    def __init__(self, plist_path: Path, *, max_bytes: int = 1024 * 1024, min_interval_seconds: float = 0.0) -> None:
        if max_bytes <= 0 or min_interval_seconds < 0:
            raise ValueError("invalid plist collector budget")
        self.plist_path = Path(plist_path)
        self.source_id = asset_source_id(self.plist_path)
        self.max_bytes = max_bytes
        self.min_interval_seconds = min_interval_seconds
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        if not target_allows_asset(target, self.plist_path):
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
            self._last_collection = now
        try:
            metadata = self.plist_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > self.max_bytes:
                return failed_batch(target, self.name, "plist_path_rejected", source_id=self.source_id)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.plist_path, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return failed_batch(target, self.name, "plist_unavailable", source_id=self.source_id)
        if not isinstance(data, dict) or not isinstance(data.get("Label"), str):
            return failed_batch(target, self.name, "plist_invalid", source_id=self.source_id)
        expected_label = target.spec.labels.get("service_label")
        if expected_label and data["Label"] != expected_label:
            return failed_batch(target, self.name, "plist_label_mismatch", source_id=self.source_id)
        command_fields = {
            "Program": data.get("Program"),
            "ProgramArguments": data.get("ProgramArguments"),
            "RunAtLoad": data.get("RunAtLoad"),
            "KeepAlive": data.get("KeepAlive"),
        }
        canonical = json.dumps(command_fields, sort_keys=True, default=str, separators=(",", ":"))
        fingerprint = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        observed_at = utc_now()
        signal = redact_signal(
            RawSignal(
                target_id=target.target_id,
                collector=self.name,
                signal_type="launchd.configuration",
                observed_at=observed_at,
                payload={"label": data["Label"], "configuration_fingerprint": fingerprint},
            )
        )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=(signal,),
            health=CollectorHealth(healthy=True),
            source_id=self.source_id,
        )
