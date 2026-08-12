"""Stable typed contracts for Memory Duo broker callers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Sequence


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    UNVERIFIED = "unverified"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    NEEDS_ATTENTION = "needs_attention"


class Verification(str, Enum):
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    SOURCE_SUPPORTED = "source_supported"
    DIRECTLY_OBSERVED = "directly_observed"
    USER_CONFIRMED = "user_confirmed"


class Authority(str, Enum):
    AGENT = "agent"
    TOOL = "tool"
    SOURCE = "source"
    USER = "user"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: str
    content: str
    source: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class MemoryCandidate:
    content: str
    memory_type: str = "fact"
    scope: str = "global"
    authority: Authority = Authority.AGENT
    verification: Verification = Verification.UNVERIFIED
    evidence: tuple[EvidenceRecord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: str
    content: str
    memory_type: str
    scope: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    authority: Authority = Authority.AGENT
    verification: Verification = Verification.UNVERIFIED
    confidence: float = 0.0
    importance: float = 0.0
    evidence_ids: tuple[str, ...] = ()
    relationships: tuple[str, ...] = ()
    source_session_id: str = ""
    task_id: str = ""
    project_id: str = ""
    child_session_id: str = ""
    mission_id: str = ""
    agent_id: str = ""
    created_at: str = field(default="", compare=False)
    updated_at: str = field(default="", compare=False)


@dataclass(frozen=True)
class MemoryEvent:
    event_type: str
    content: str = ""
    mission_id: str = ""
    task_id: str = ""
    agent_id: str = ""
    parent_agent_id: str = ""
    workspace_id: str = ""
    project_id: str = ""
    session_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievalRequest:
    query: str
    scope: str = "global"
    session_id: str = ""
    max_memories: int = 12
    max_tokens: int = 5000


@dataclass(frozen=True)
class MemoryPacket:
    memories: tuple[MemoryRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    conflicts: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    no_verified_memory: bool = False


@dataclass(frozen=True)
class CandidateDecision:
    action: str
    memory_id: Optional[str] = None
    reason: str = ""


@dataclass(frozen=True)
class BrokerStatus:
    state: str = "UNAVAILABLE"
    indexed_notes: int = 0
    pending_events: int = 0
    incomplete_transactions: int = 0
    message: str = ""
