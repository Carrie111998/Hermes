from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
import hashlib, json
from types import MappingProxyType

def _freeze(v):
    if isinstance(v, Mapping): return MappingProxyType({str(k): _freeze(x) for k,x in v.items()})
    if isinstance(v, (list, tuple)): return tuple(_freeze(x) for x in v)
    return v

@dataclass(frozen=True)
class IncidentSignal:
    signal_id: str
    target_id: str
    collector: str
    signal_type: str
    observed_at: datetime
    payload: Mapping[str, Any]
    severity: str = "warning"
    source_id: str = ""
    def __post_init__(self):
        if not self.signal_id: raise ValueError("signal_id required")
        object.__setattr__(self, "payload", _freeze(self.payload))

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
    history: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class ReviewResult:
    incident_fingerprint: str
    decision: str
    rationale: str
    model_used: bool = False
    degraded: bool = False
    hypotheses: tuple[str, ...] = ()
    risk: str = "unknown"
    actions: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()
    confidence: float = 0.0
    evidence_ids: tuple[str, ...] = ()
