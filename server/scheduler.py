"""Daily digest scheduler.

A single daemon thread that wakes on an interval, and for each active tenant
writes the morning plan and evening report once their hour has passed.

Why a plain thread and not APScheduler or cron: pyproject.toml pins every
dependency exactly and states the rule — "smaller `dependencies` = smaller blast
radius for the next supply-chain attack". A scheduler that needs one timer and
one idempotent write does not justify a new package, and the codebase already
runs background work on a thread pool owned by AgentRunService.

The loop is deliberately dumb and idempotent. It holds no state between ticks,
never assumes it ran yesterday, and relies on the UNIQUE(company_id,
digest_date, kind) constraint to make a duplicate tick a no-op. That means a
restart, a clock jump, or a machine that was asleep at 08:00 all behave the
same: the next tick after the hour writes the digest exactly once.

It only ever writes digests. It does not start agent runs, and it never sends
anything — delivery stays behind human approval.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from .db import Database
from .digest import PLAN, REPORT, day_key, write_digest

logger = logging.getLogger("interfaze.scheduler")


class DailyDigestScheduler:
    def __init__(
        self,
        db: Database,
        *,
        plan_hour: int = 8,
        report_hour: int = 18,
        interval_seconds: int = 300,
    ) -> None:
        self.db = db
        self.plan_hour = plan_hour
        self.report_hour = report_hour
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="interfaze-digest", daemon=True,
        )
        self._thread.start()
        logger.info(
            "daily digest scheduler started (plan %02d:00, report %02d:00)",
            self.plan_hour, self.report_hour,
        )

    def shutdown(self, wait: bool = False) -> None:
        self._stop.set()
        if wait and self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        # tick() immediately so a server started after the plan hour still has
        # today's briefing, rather than waiting a full interval for one.
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                # A scheduler must never die on one bad tenant; the next tick
                # retries, and the digest write is idempotent.
                logger.exception("digest tick failed")
            self._stop.wait(self.interval_seconds)

    def tick(self, moment: float | None = None) -> int:
        """Write any digest whose hour has passed today. Returns how many were
        written. Safe to call directly — this is what the tests drive."""
        stamp = dt.datetime.fromtimestamp(moment) if moment is not None else dt.datetime.now()
        date = day_key(moment)
        due = [kind for kind, hour in ((PLAN, self.plan_hour), (REPORT, self.report_hour))
               if stamp.hour >= hour]
        if not due:
            return 0
        companies = [row["id"] for row in self.db.all(
            "SELECT id FROM companies WHERE status='active' ORDER BY id", ()
        )]
        written = 0
        for company_id in companies:
            for kind in due:
                try:
                    before = self.db.one(
                        "SELECT 1 FROM daily_digests WHERE company_id=? AND digest_date=? AND kind=?",
                        (company_id, date, kind),
                    )
                    if before:
                        continue
                    write_digest(self.db, company_id, date, kind)
                    written += 1
                except Exception:
                    logger.exception("could not write %s digest for %s", kind, company_id)
        return written
