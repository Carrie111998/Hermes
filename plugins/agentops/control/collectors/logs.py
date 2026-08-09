"""Bounded, source-aware regular-file log observation with lossless cursors."""

from __future__ import annotations

import os
import re
import stat
import threading
import time
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
    asset_source_id,
    target_allows_asset,
    utc_now,
)
from plugins.agentops.control.redaction import redact_log_line, redact_signal


_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+\b")
_PID = re.compile(r"\b(?:pid|process)[=: ]\d+\b", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", re.I)
_SPACE = re.compile(r"\s+")


class LogCollector:
    """Read exactly one approved log asset; it never edits or rotates it."""

    def __init__(
        self,
        name: str,
        path: Path,
        *,
        max_bytes: int = 64 * 1024,
        max_lines: int = 200,
        max_line_bytes: int = 16 * 1024,
        min_interval_seconds: float = 0.0,
    ) -> None:
        if not name or max_bytes <= 0 or max_lines <= 0 or max_line_bytes <= 0 or min_interval_seconds < 0:
            raise ValueError("invalid log collector configuration")
        self.name = name
        self.path = Path(path)
        self.source_id = asset_source_id(self.path)
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self.max_line_bytes = max_line_bytes
        self.min_interval_seconds = min_interval_seconds
        self._last_collection = 0.0
        self._rate_lock = threading.Lock()
        self.last_identity: tuple[int, int] | None = None

    @staticmethod
    def _normalize_message(line: str) -> str:
        normalized = _TIMESTAMP.sub("<time>", line)
        normalized = _PID.sub("pid=<id>", normalized)
        normalized = _UUID.sub("<id>", normalized)
        return _SPACE.sub(" ", normalized).strip()

    def _allow_collection(self) -> bool:
        with self._rate_lock:
            now = time.monotonic()
            if now - self._last_collection < self.min_interval_seconds:
                return False
            self._last_collection = now
            return True

    def _consume_lines(self, raw: bytes) -> tuple[list[str], int]:
        """Return only complete/explicitly bounded lines and their byte extent."""
        lines: list[str] = []
        consumed = 0
        while consumed < len(raw) and len(lines) < self.max_lines:
            newline = raw.find(b"\n", consumed)
            if newline < 0:
                remaining = len(raw) - consumed
                if remaining >= self.max_line_bytes:
                    segment = raw[consumed : consumed + self.max_line_bytes]
                    lines.append(segment.decode("utf-8", errors="replace"))
                    consumed += len(segment)
                break
            segment = raw[consumed:newline]
            consumed = newline + 1
            if segment.endswith(b"\r"):
                segment = segment[:-1]
            lines.append(segment.decode("utf-8", errors="replace"))
        return lines, consumed

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch:
        if not target_allows_asset(target, self.path):
            return failed_batch(target, self.name, "asset_unbound", source_id=self.source_id)
        if not self._allow_collection():
            return failed_batch(target, self.name, "collector_rate_limited", source_id=self.source_id)
        try:
            metadata = self.path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                return failed_batch(target, self.name, "log_path_rejected", source_id=self.source_id)
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.path, flags)
        except OSError:
            return failed_batch(target, self.name, "log_unavailable", source_id=self.source_id)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                return failed_batch(target, self.name, "log_path_rejected", source_id=self.source_id)
            self.last_identity = (int(opened.st_dev), int(opened.st_ino))
            if cursor is not None and cursor.source_id not in ("", self.source_id):
                cursor = None
                reset_reason = CursorResetReason.SOURCE_CHANGED
            else:
                reset_reason = None
            decision = advance_log_cursor(cursor, opened)
            if reset_reason is not None:
                decision = type(decision)(offset=0, reason=reset_reason)
            os.lseek(descriptor, decision.offset, os.SEEK_SET)
            raw = os.read(descriptor, self.max_bytes)
        except OSError:
            return failed_batch(target, self.name, "log_unavailable", source_id=self.source_id)
        finally:
            os.close(descriptor)

        lines, consumed_bytes = self._consume_lines(raw)
        observed_at = utc_now()
        signals = []
        for line in lines:
            payload = redact_log_line(line)
            if "message" in payload:
                payload["message"] = self._normalize_message(payload["message"])
            if not payload.get("message") and "record" not in payload:
                continue
            signals.append(
                redact_signal(
                    RawSignal(
                        target_id=target.target_id,
                        collector=self.name,
                        signal_type="log.line",
                        observed_at=observed_at,
                        payload=payload,
                    )
                )
            )
        if decision.reason is not CursorResetReason.CONTINUE:
            signals.append(
                redact_signal(
                    RawSignal(
                        target_id=target.target_id,
                        collector=self.name,
                        signal_type="log.cursor_reset",
                        observed_at=observed_at,
                        payload={"reason": decision.reason.value},
                    )
                )
            )
        return CollectionBatch(
            target_id=target.target_id,
            collector=self.name,
            collected_at=observed_at,
            signals=tuple(signals),
            health=CollectorHealth(healthy=True),
            next_cursor=LogCursor(
                inode=int(opened.st_ino), offset=decision.offset + consumed_bytes, source_id=self.source_id
            ),
            source_id=self.source_id,
        )
