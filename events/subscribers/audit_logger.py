"""AuditLogger subscriber — append-only JSONL event trail.

Records every event for debugging and replay.  Rotated weekly into
the audit/ archive directory, retained for 90 days.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from events.bus import EventBus
from events.schema import Event
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

ROTATION_INTERVAL = 604800  # 7 days in seconds
RETENTION_DAYS = 90
# Force rotation if the live audit.jsonl exceeds this size, regardless of age.
# Set to 256 MiB after the 2026-04-28 incident produced a 459 MB file inside
# the 7-day age window. Operators can scan a 256 MB rotated file in a reasonable
# time; anything larger is a sign of a flood that should be visible per-day.
SIZE_CAP_BYTES = 256 * 1024 * 1024  # 256 MiB


class AuditLogger(BaseSubscriber):
    subscriber_id = "audit-logger"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus, audit_path: Optional[Path] = None):
        super().__init__(bus)
        if audit_path is None:
            from events.paths import audit_log_path
            audit_path = audit_log_path()
        self.audit_path = Path(audit_path)
        self._archive_dir = self.audit_path.parent / "audit"
        self._last_rotation_check: float = 0

    def handle(self, event: Event) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        # Check rotation once per hour (not on every event)
        now = time.monotonic()
        if now - self._last_rotation_check > 3600:
            self._rotate_if_needed()
            self._cleanup_old_archives()
            self._last_rotation_check = now

    def _rotate_if_needed(self) -> None:
        """Rotate audit.jsonl weekly into the archive directory."""
        if not self.audit_path.exists():
            return
        try:
            stat = self.audit_path.stat()
            age = time.time() - stat.st_mtime
            # Rotate when EITHER (a) age exceeds the weekly interval OR (b) size
            # exceeds the safety cap. The size cap was added 2026-04-28 after a
            # subscriber re-fire loop produced 459 MB inside the 7-day window.
            if age < ROTATION_INTERVAL and stat.st_size < SIZE_CAP_BYTES:
                return
            if stat.st_size == 0:
                return

            self._archive_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            archive_name = f"audit-{date_str}.jsonl"
            dest = self._archive_dir / archive_name

            # Avoid overwriting
            counter = 1
            while dest.exists():
                dest = self._archive_dir / f"audit-{date_str}-{counter}.jsonl"
                counter += 1

            self.audit_path.rename(dest)
            logger.info("AuditLogger: rotated to %s", dest.name)
        except Exception as e:
            logger.warning("AuditLogger: rotation failed: %s", e)

    def _cleanup_old_archives(self) -> None:
        """Remove archive files older than 90 days."""
        if not self._archive_dir.exists():
            return
        try:
            cutoff = time.time() - (RETENTION_DAYS * 86400)
            for f in self._archive_dir.glob("audit-*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info("AuditLogger: purged old archive %s", f.name)
        except Exception as e:
            logger.warning("AuditLogger: archive cleanup failed: %s", e)
