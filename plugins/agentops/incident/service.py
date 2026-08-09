from __future__ import annotations
from datetime import datetime
from .correlator import IncidentCorrelator
from .digest import digest
from .models import IncidentSignal, ReviewResult
from .notifier import NotificationGate
from .review import RuleReviewer

class IncidentOpsService:
    def __init__(self, *, window_seconds: int = 900, max_incidents: int = 1000, max_history: int = 5000) -> None:
        self.correlator = IncidentCorrelator(window_seconds=window_seconds, max_incidents=max_incidents, max_history=max_history)
        self.reviewer = RuleReviewer(); self.notifier = NotificationGate()
    def ingest(self, signal: IncidentSignal): return self.correlator.ingest(signal)
    def merge(self, target, source): return self.correlator.merge(target, source)
    def split(self, incident, signal_ids): return self.correlator.split(incident, signal_ids)
    def suppress(self, fingerprint): return self.correlator.suppress(fingerprint)
    def review(self, incident) -> ReviewResult: return self.reviewer.review(incident)
    def notify(self, incident, now: datetime) -> bool: return self.notifier.allow(incident, now)
    def digest(self, period: str, generated_at: datetime): return digest(list(self.correlator.all_incidents()), period=period, generated_at=generated_at)
