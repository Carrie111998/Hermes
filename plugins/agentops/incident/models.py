from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping
import hashlib, json
import re
from plugins.agentops.control.redaction import redact_value, contains_secret
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
        if type(self.signal_id) is not str or not self.signal_id or type(self.target_id) is not str or not self.target_id:
            raise ValueError("signal identity required")
        if type(self.collector) is not str or type(self.signal_type) is not str or not isinstance(self.payload, Mapping):
            raise ValueError("invalid signal schema")
        safe = redact_value(self.payload)
        if contains_secret(safe): raise ValueError("secret in incident evidence")
        object.__setattr__(self, "payload", _freeze(safe))

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
        if not isinstance(self.incident_fingerprint, str) or not self.incident_fingerprint:
            raise ValueError("invalid incident fingerprint")
        if type(self.decision) is not str or self.decision not in {"observe", "escalate"}:
            raise ValueError("invalid review decision")
        if type(self.rationale) is not str:
            raise ValueError("invalid review rationale")
        if type(self.risk) is not str or self.risk not in {"low", "medium", "high", "critical", "unknown"}:
            raise ValueError("invalid review risk")
        if type(self.confidence) is not float or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("invalid review confidence")
        if type(self.model_used) is not bool or type(self.degraded) is not bool:
            raise ValueError("invalid review flags")
        fields = (self.hypotheses, self.actions, self.verification, self.rollback, self.evidence_ids)
        if any(type(items) is not tuple or any(type(item) is not str for item in items) for items in fields):
            raise ValueError("invalid review fields")
        if self.degraded and (self.model_used or self.actions):
            raise ValueError("degraded review cannot propose actions/model")
