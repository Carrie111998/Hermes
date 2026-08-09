from __future__ import annotations
from datetime import datetime, timedelta
from .models import Incident

class NotificationGate:
    def __init__(self, *, min_interval_seconds: int = 900, max_per_period: int = 20) -> None:
        self.interval = timedelta(seconds=min_interval_seconds); self.max_per_period = max_per_period; self.sent = 0; self._period = None; self._period_max = None
    def allow(self, incident: Incident, now: datetime) -> bool:
        period = now.date()
        if self._period_max is None: self._period_max = period
        if period > self._period_max: self._period_max = period; self.sent = 0
        if period < self._period_max: return False
        if self.sent >= self.max_per_period: return False
        if incident.last_notified_at and now - incident.last_notified_at < self.interval: return False
        incident.last_notified_at = now; incident.notification_count += 1; self.sent += 1; return True
