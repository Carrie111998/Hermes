"""AuditLogger subscriber — append-only JSONL event trail.

Records every event for debugging and replay.

Rotation of the live audit.jsonl is OWNED by the external daily cron
``audit-rotate`` (~/.hermes/scripts/audit_rotate.py): a tail-preserving trim
that keeps the newest ~16 days live and moves older lines into per-day
archives (events/audit/audit-YYYY-MM-DD.jsonl). Tail consumers — curator
heartbeat_bootstrap (needs >=10 days), critic_retro (14), cron_stall_detector
/ scribe_digest / effort tuner (7) — read that window from the live file, so
this subscriber must never rotate it wholesale in normal operation.

The only rotation kept here is the 256 MiB size cap, as an emergency
backstop. It is unreachable while the cron runs (steady-state file ~40 MB),
and it bare-renames the file into the archive dir — emptying the live tail —
so it firing means the cron is broken: fix the cron, don't rely on the cap.
(A weekly age arm existed 2026-04-16..2026-07-13 but was dead code from
birth — handle() refreshed st_mtime microseconds before every check, so age
was always ~0. It was removed rather than fixed to avoid a second active
rotation mechanism.)

Archives older than 90 days are purged here (hourly-gated), in parity with
the cron's filename-date retention.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from events.bus import EventBus
from events.schema import Event
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90
# Emergency backstop only — NOT the normal rotation path (see module
# docstring). Sized at 256 MiB after the 2026-04-28 incident produced a
# 459 MB file in 7 days; the audit-rotate cron keeps the steady-state file
# more than 6x below this.
SIZE_CAP_BYTES = 256 * 1024 * 1024  # 256 MiB


class AuditLogger(BaseSubscriber):
    subscriber_id = "audit-logger"
    poll_interval_seconds = 5
    # Manages its own cursor seed in startup() (at 0, for a gap-free forensic
    # trail) — opt out of the construction-time head-seed so startup()'s
    # INSERT OR IGNORE at 0 isn't blocked by a pre-existing row.
    SEED_CURSOR_AT_CONSTRUCTION = False

    def __init__(self, bus: EventBus, audit_path: Optional[Path] = None):
        super().__init__(bus)
        if audit_path is None:
            from events.paths import audit_log_path
            audit_path = audit_log_path()
        self.audit_path = Path(audit_path)
        self._archive_dir = self.audit_path.parent / "audit"
        self._last_rotation_check: float = 0

    def startup(self) -> None:
        """Seed the cursor row at last_rowid=0 so the audit trail is gap-free.

        AuditLogger is the operator-facing forensic record — it MUST capture
        every event that lands in the bus, including ones emitted in the
        first few seconds of gateway life before any non-empty poll has
        happened. Without an explicit seed, ``bus.subscribe()``'s
        first-call default (per the 2026-04-28 scribe backlog-flood fix)
        jumps to MAX(rowid)=current bus head and silently skips those
        early events. ``bus.ack([])`` short-circuits, so no cursor row is
        ever inserted by the normal poll path until something is acked.

        INSERT OR IGNORE leaves any pre-existing cursor row alone, so a
        gateway restart with persistent state continues from where it
        last acked instead of replaying the whole bus.
        """
        conn = self.bus._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO subscriber_cursors "
            "(subscriber_id, last_rowid) VALUES (?, 0)",
            (self.subscriber_id,),
        )
        conn.commit()

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
        """Emergency size-cap backstop — NOT the normal rotation path.

        The external audit-rotate cron owns rotation (daily tail-preserving
        trim). This bare-rename empties the live tail that curator / critic /
        scribe consumers read, so it must only ever fire if the cron has been
        broken long enough for the file to blow past SIZE_CAP_BYTES.
        """
        if not self.audit_path.exists():
            return
        try:
            if self.audit_path.stat().st_size < SIZE_CAP_BYTES:
                return

            self._archive_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            dest = self._archive_dir / f"audit-{date_str}.jsonl"

            # Avoid overwriting
            counter = 1
            while dest.exists():
                dest = self._archive_dir / f"audit-{date_str}-{counter}.jsonl"
                counter += 1

            self.audit_path.rename(dest)
            logger.warning(
                "AuditLogger: size-cap backstop rotated to %s — the audit-rotate "
                "cron should have trimmed the file long before this; check why "
                "it isn't running",
                dest.name,
            )
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
