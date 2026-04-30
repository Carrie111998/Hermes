"""CronTriggerLog subscriber — per-job rolling JSONL of cron_triggered events.

Mirrors the AuditLogger pattern but consumes ONLY cron_triggered events,
giving operators a focused, easy-to-grep artifact for postmortem
attribution of off-schedule cron fires.

Storage: events/cron_triggers.jsonl (canonical root, cross-profile)
Rotation: weekly into events/audit/cron_triggers-YYYY-MM-DD.jsonl
Retention: 30 days
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

ROTATION_INTERVAL = 604800  # 7 days
RETENTION_DAYS = 30


class CronTriggerLog(BaseSubscriber):
    subscriber_id = "cron-trigger-log"
    poll_interval_seconds = 5
    event_types: List[EventType] = [EventType.CRON_TRIGGERED]

    def __init__(self, bus: EventBus, log_path: Optional[Path] = None):
        super().__init__(bus)
        if log_path is None:
            from events.paths import cron_trigger_log_path
            log_path = cron_trigger_log_path()
        self.log_path = Path(log_path)
        self._archive_dir = self.log_path.parent / "audit"
        self._last_rotation_check: float = 0

    def handle(self, event: Event) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        now = time.monotonic()
        if now - self._last_rotation_check > 3600:
            self._rotate_if_needed()
            self._cleanup_old_archives()
            self._last_rotation_check = now

    def _rotate_if_needed(self) -> None:
        if not self.log_path.exists():
            return
        try:
            stat = self.log_path.stat()
            age = time.time() - stat.st_mtime
            if age < ROTATION_INTERVAL:
                return
            if stat.st_size == 0:
                return

            self._archive_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            dest = self._archive_dir / f"cron_triggers-{date_str}.jsonl"

            counter = 1
            while dest.exists():
                dest = self._archive_dir / f"cron_triggers-{date_str}-{counter}.jsonl"
                counter += 1

            self.log_path.rename(dest)
            logger.info("CronTriggerLog: rotated to %s", dest.name)
        except Exception:
            logger.exception("CronTriggerLog: rotation failed")

    def _cleanup_old_archives(self) -> None:
        if not self._archive_dir.exists():
            return
        try:
            cutoff = time.time() - (RETENTION_DAYS * 86400)
            for f in self._archive_dir.glob("cron_triggers-*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info("CronTriggerLog: purged old archive %s", f.name)
        except Exception:
            logger.exception("CronTriggerLog: archive cleanup failed")
