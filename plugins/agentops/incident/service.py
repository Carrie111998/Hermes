from __future__ import annotations
from datetime import datetime
from .correlator import IncidentCorrelator
from .digest import digest
from .models import IncidentSignal, ReviewResult
from .notifier import NotificationGate
from .review import RuleReviewer

class IncidentOpsService:
    def __init__(self, *, window_seconds: int = 900, max_incidents: int = 1000) -> None:
        self.correlator = IncidentCorrelator(window_seconds=window_seconds, max_incidents=max_incidents)
        self.reviewer = RuleReviewer(); self.notifier = NotificationGate()
    def ingest(self, signal: IncidentSignal): return self.correlator.ingest(signal)
    def review(self, incident) -> ReviewResult: return self.reviewer.review(incident)
    def notify(self, incident, now: datetime) -> bool: return self.notifier.allow(incident, now)
    def digest(self, period: str, generated_at: datetime): return digest(list(self.correlator.incidents()), period=period, generated_at=generated_at)
