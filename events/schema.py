"""Event schema definitions for the Hermes Event Bus.

Defines the typed event envelope, event type catalog with default priorities,
and priority levels used for notification routing.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class Priority(Enum):
    """Event priority levels for notification routing.

    Each level has a numeric value for comparison and filtering.
    """

    CRITICAL = ("critical", 40)
    HIGH = ("high", 30)
    NORMAL = ("normal", 20)
    LOW = ("low", 10)

    def __init__(self, label: str, level: int):
        self.label = label
        self.level = level

    @classmethod
    def from_string(cls, value: str) -> "Priority":
        """Parse a priority string, falling back to NORMAL for unknown values."""
        lookup = {p.label: p for p in cls}
        return lookup.get(value.lower(), cls.NORMAL)


class EventType(Enum):
    """Catalog of all event types emitted by the Hermes Event Bus.

    Each member is a tuple of (event_type_string, default_priority).
    """

    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW)
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL)
    CRON_FAILED = ("cron_failed", Priority.HIGH)
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL)

    # Job discovery & scoring
    JOB_DISCOVERED = ("job_discovered", Priority.NORMAL)
    JOB_SCORED = ("job_scored", Priority.NORMAL)
    JOB_HIGH_SCORE = ("job_high_score", Priority.HIGH)
    JOB_VIP_DISCOVERED = ("job_vip_discovered", Priority.HIGH)

    # Tailoring & applications
    TAILOR_COMPLETED = ("tailor_completed", Priority.NORMAL)
    APPLICATION_READY = ("application_ready", Priority.HIGH)
    APPLICATION_SUBMITTED = ("application_submitted", Priority.HIGH)
    APPLICATION_FAILED = ("application_failed", Priority.CRITICAL)
    APPLICATION_BLOCKED = ("application_blocked", Priority.CRITICAL)

    # Pipeline tracking
    STAGE_TRANSITION = ("stage_transition", Priority.NORMAL)
    INTERVIEW_SIGNAL = ("interview_signal", Priority.CRITICAL)
    OFFER_SIGNAL = ("offer_signal", Priority.CRITICAL)
    FOLLOWUP_DUE = ("followup_due", Priority.HIGH)

    # System
    DIGEST_GENERATED = ("digest_generated", Priority.LOW)
    GATEWAY_HEALTH = ("gateway_health", Priority.HIGH)
    AGENT_ERROR = ("agent_error", Priority.HIGH)
    MEMORY_CONSOLIDATED = ("memory_consolidated", Priority.LOW)
    SKILL_EVOLVED = ("skill_evolved", Priority.LOW)
    MAILBOX_MESSAGE = ("mailbox_message", Priority.LOW)

    def __init__(self, type_string: str, default_priority: Priority):
        self.type_string = type_string
        self.default_priority = default_priority

    @classmethod
    def from_string(cls, value: str) -> Optional["EventType"]:
        """Look up an EventType by its string name. Returns None if not found."""
        lookup = {et.type_string: et for et in cls}
        return lookup.get(value.lower())


@dataclass
class Event:
    """A single typed event in the Hermes Event Bus.

    Events are the universal unit of communication between producers
    (cron jobs, agents, health monitors) and subscribers (Telegram notifier,
    WhatsApp escalator, memory writer, etc.).
    """

    event_id: str
    event_type: EventType
    source: str
    timestamp: str  # ISO8601 UTC
    priority: Priority
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    job_id: Optional[str] = None
    tags: List[str] = field(default_factory=list)

    @classmethod
    def create(
        cls,
        event_type: EventType,
        source: str,
        payload: Dict[str, Any],
        priority: Optional[Priority] = None,
        correlation_id: Optional[str] = None,
        job_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> "Event":
        """Create a new event with auto-generated ID and timestamp."""
        return cls(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            source=source,
            timestamp=datetime.now(timezone.utc).isoformat(),
            priority=priority or event_type.default_priority,
            payload=payload,
            correlation_id=correlation_id,
            job_id=job_id,
            tags=tags or [],
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.type_string,
            "source": self.source,
            "timestamp": self.timestamp,
            "priority": self.priority.label,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "job_id": self.job_id,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Event":
        """Deserialize from a dict (e.g., from SQLite JSON or audit log)."""
        event_type = EventType.from_string(data["event_type"])
        if event_type is None:
            raise ValueError(f"Unknown event type: {data['event_type']}")
        return cls(
            event_id=data["event_id"],
            event_type=event_type,
            source=data["source"],
            timestamp=data["timestamp"],
            priority=Priority.from_string(data["priority"]),
            payload=data.get("payload", {}),
            correlation_id=data.get("correlation_id"),
            job_id=data.get("job_id"),
            tags=data.get("tags", []),
        )
