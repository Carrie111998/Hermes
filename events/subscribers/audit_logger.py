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


class AuditLogger(BaseSubscriber):
    subscriber_id = "audit-logger"
    poll_interval_seconds = 5

    def __init__(self, bus: EventBus, audit_path: Optional[Path] = None):
        super().__init__(bus)
        if audit_path is None:
            from hermes_constants import get_hermes_home
            audit_path = get_hermes_home() / "events" / "audit.jsonl"
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
            if age < ROTATION_INTERVAL:
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
