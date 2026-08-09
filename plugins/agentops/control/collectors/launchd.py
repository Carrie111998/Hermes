"""Read plist configuration facts without changing service state."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import stat
from pathlib import Path

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


class LaunchdCollector:
    """Read the declared plist only; runtime control is intentionally absent."""

    name = "launchd"

    def __init__(self, plist_path: Path) -> None:
        self.plist_path = Path(plist_path)

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        try:
            metadata = self.plist_path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return failed_batch(target, self.name, "plist_path_rejected")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.plist_path, flags)
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return failed_batch(target, self.name, "plist_unavailable")
        if not isinstance(data, dict) or not isinstance(data.get("Label"), str):
            return failed_batch(target, self.name, "plist_invalid")
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
        )
