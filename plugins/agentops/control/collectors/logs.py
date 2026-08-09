"""Bounded regular-file log observation with inode/offset cursors."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from plugins.agentops.control.collectors.base import failed_batch
from plugins.agentops.control.cursors import advance_log_cursor
from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    CursorResetReason,
    LogCursor,
    RawSignal,
    Target,
    utc_now,
)
from plugins.agentops.control.redaction import redact_signal


_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+\b")
_PID = re.compile(r"\b(?:pid|process)[=: ]\d+\b", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
_SPACE = re.compile(r"\s+")


class LogCollector:
    """Read one approved log file; it never creates, rotates, or edits it."""

    def __init__(self, name: str, path: Path, *, max_bytes: int = 64 * 1024, max_lines: int = 200) -> None:
        if not name or max_bytes <= 0 or max_lines <= 0:
            raise ValueError("invalid log collector configuration")
        self.name = name
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_lines = max_lines

    @staticmethod
    def _normalize_line(line: str) -> str:
        normalized = _TIMESTAMP.sub("<time>", line)
        normalized = _PID.sub("pid=<id>", normalized)
        normalized = _UUID.sub("<id>", normalized)
        return _SPACE.sub(" ", normalized).strip()

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        try:
            metadata = self.path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return failed_batch(target, self.name, "log_path_rejected")
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
        except OSError:
            return failed_batch(target, self.name, "log_unavailable")
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return failed_batch(target, self.name, "log_path_rejected")
            decision = advance_log_cursor(cursor, opened)
            os.lseek(descriptor, decision.offset, os.SEEK_SET)
            raw = os.read(descriptor, self.max_bytes)
        except OSError:
            return failed_batch(target, self.name, "log_unavailable")
        finally:
            os.close(descriptor)

        lines = raw.decode("utf-8", errors="replace").splitlines()[: self.max_lines]
        observed_at = utc_now()
        signals = tuple(
            redact_signal(
                RawSignal(
                    target_id=target.target_id,
                    collector=self.name,
                    signal_type="log.line",
                    observed_at=observed_at,
                    payload={"message": self._normalize_line(line)},
                )
            )
            for line in lines
            if line.strip()
        )
        if decision.reason is not CursorResetReason.CONTINUE:
            signals = signals + (
                redact_signal(
                    RawSignal(
                        target_id=target.target_id,
                        collector=self.name,
                        signal_type="log.cursor_reset",
                        observed_at=observed_at,
                        payload={"reason": decision.reason.value},
                    )
                ),
            )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=signals,
            health=CollectorHealth(healthy=True),
            next_cursor=LogCursor(inode=int(opened.st_ino), offset=decision.offset + len(raw)),
        )
