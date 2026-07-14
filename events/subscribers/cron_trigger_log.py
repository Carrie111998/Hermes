"""CronTriggerLog subscriber — per-job rolling JSONL of cron_triggered events.

Mirrors the AuditLogger pattern but consumes ONLY cron_triggered events,
giving operators a focused, easy-to-grep artifact for postmortem
attribution of off-schedule cron fires.

Storage: events/cron_triggers.jsonl (canonical root, cross-profile)

The live file is append-only and is never rotated. A weekly age-rotation
arm existed f6c823e24 (2026-04-30) .. 2026-07-13 but was dead code from
birth — handle() appends (refreshing st_mtime) microseconds before every
hourly-gated check, so ``time.time() - st_mtime`` was always ~0 and no
cron_triggers-* archive was ever produced. It was removed rather than
fixed (AuditLogger precedent, edfed44c8): the file only receives
cron_triggered events (~KB/week; 4.7 KB after its first 10 weeks), so
append-only keeps the full fire history greppable in one place and is
decades from being a size concern. If the growth rate ever changes, fold
the file into the external audit-rotate cron
(~/.hermes/scripts/audit_rotate.py) rather than resurrecting in-process
rotation.

The 30-day retention sweep of cron_triggers-*.jsonl under events/audit/
remains (hourly-gated) for anything manually placed there — nothing
creates such archives anymore.
"""

import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

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
        self._last_cleanup_check: float = 0

    def handle(self, event: Event) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        now = time.monotonic()
        if now - self._last_cleanup_check > 3600:
            self._cleanup_old_archives()
            self._last_cleanup_check = now

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
