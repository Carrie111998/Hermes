from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

@dataclass(frozen=True)
class IncidentSignal:
    target_id: str
    collector: str
    signal_type: str
    observed_at: datetime
    payload: Mapping[str, Any]
    severity: str = "warning"
    source_id: str = ""

@dataclass
class Incident:
    fingerprint: str
    first_seen: datetime
    last_seen: datetime
    targets: set[str] = field(default_factory=set)
    signal_count: int = 0
    severity: str = "warning"
    state: str = "open"
    notification_count: int = 0
    last_notified_at: datetime | None = None
    evidence: list[IncidentSignal] = field(default_factory=list)

@dataclass(frozen=True)
class ReviewResult:
    incident_fingerprint: str
    decision: str
    rationale: str
    model_used: bool = False
    degraded: bool = False
