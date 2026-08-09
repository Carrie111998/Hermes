from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
import hashlib, json
import re
from types import MappingProxyType

def _freeze(v):
    if isinstance(v, Mapping): return MappingProxyType({str(k): _freeze(x) for k,x in v.items()})
    if isinstance(v, (list, tuple)): return tuple(_freeze(x) for x in v)
    return v

def _redact(v):
    if isinstance(v, Mapping): return {str(k): ("[REDACTED]" if re.search(r"password|token|secret|api[_-]?key", str(k), re.I) else _redact(x)) for k,x in v.items()}
    if isinstance(v, (list, tuple)): return [_redact(x) for x in v]
    if isinstance(v, str): return re.sub(r"(?i)(password|token|secret)\s*[:=]\s*[^,\s]+", r"\1=[REDACTED]", v)
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
        object.__setattr__(self, "payload", _freeze(_redact(self.payload)))

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
    def __post_init__(self):
        if self.decision not in {"observe", "escalate"} or self.risk not in {"low", "medium", "high", "critical", "unknown"} or not 0 <= self.confidence <= 1: raise ValueError("invalid review proposal")
        if self.actions and self.degraded or (self.degraded and self.model_used): raise ValueError("degraded review cannot propose actions/model")
        if not isinstance(self.model_used, bool) or not isinstance(self.degraded, bool) or not all(isinstance(x, str) for x in self.hypotheses + self.actions + self.verification + self.rollback + self.evidence_ids): raise ValueError("invalid review schema")
