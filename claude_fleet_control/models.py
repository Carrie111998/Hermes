"""Frozen, JSON-serializable records for the P6 fleet controller.

Everything here is immutable data. The planner consumes and produces these;
the controller serializes them into event payloads. No I/O, no clocks.

Payload hygiene contract (plan-approved): event payloads built from these
records carry process IDENTITIES (``pid:create_time``), never full command
lines, transcript contents, or anything credential-shaped. ``ProcessRecord``
itself keeps argv so the planner can classify — it just never leaves the
process through ``to_payload`` methods.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple

SCHEMA_VERSION = 1

# The three recognized modes. Anything else behaves as "disabled" — an
# unknown mode must fail toward inaction, not toward a default that acts.
MODE_DISABLED = "disabled"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
VALID_MODES = (MODE_DISABLED, MODE_SHADOW, MODE_ENFORCE)

# The one action this controller can ever project or take. Named for what
# Windows ``taskkill /T /F`` actually is — there is no graceful variant here,
# and calling it "terminate" would understate it.
ACTION_HARD_TERMINATE = "hard_terminate"


def identity_of(pid: int, create_time: float) -> str:
    """Stable process identity: PID alone is recyclable, PID+create_time is not."""
    return f"{pid}:{int(create_time)}"


@dataclass(frozen=True)
class ProcessRecord:
    """One process, as read in a single snapshot pass.

    ``complete`` means every field was read successfully. An incomplete
    member anywhere in a tree protects the WHOLE tree — unknown is protected.
    """

    pid: int
    ppid: Optional[int]
    name: str
    exe: Optional[str]
    cmdline: Tuple[str, ...]
    create_time: float
    rss: int
    username: Optional[str]
    complete: bool

    @property
    def identity(self) -> str:
        return identity_of(self.pid, self.create_time)


@dataclass(frozen=True)
class ProcessSnapshot:
    """A whole-box process census taken at one moment."""

    taken_at: float
    records: Tuple[ProcessRecord, ...]
    complete: bool  # False if the iteration itself failed partway


# Transcript resolution outcomes. Only "exact" and "fallback" are usable;
# every other resolution protects the tree.
TRANSCRIPT_EXACT = "exact"          # --resume UUID matched an existing file
TRANSCRIPT_FALLBACK = "fallback"    # newest transcript, folder unshared
TRANSCRIPT_AMBIGUOUS = "ambiguous"  # >1 live root maps to the same folder
TRANSCRIPT_MISSING = "missing"      # no cwd / no folder / no transcripts


@dataclass(frozen=True)
class TranscriptEvidence:
    resolution: str
    path: Optional[str]
    mtime: Optional[float]


@dataclass(frozen=True)
class PressureEvidence:
    """The D7 trigger, validated. ``valid`` is the only field the trigger
    logic consults; the rest is audit detail."""

    valid: bool
    reason_code: str  # ok | missing | stale | future | malformed | disarmed | tied_contradictory | bus_error
    event_id: Optional[str] = None
    event_timestamp: Optional[str] = None
    age_seconds: Optional[float] = None
    sustained_ms: Optional[float] = None

    def to_payload(self) -> Dict[str, object]:
        return {
            "valid": self.valid,
            "reason_code": self.reason_code,
            "event_id": self.event_id,
            "event_timestamp": self.event_timestamp,
            "age_seconds": self.age_seconds,
            "sustained_ms": self.sustained_ms,
        }


@dataclass(frozen=True)
class FleetPolicy:
    """The complete, versioned decision policy.

    ``digest()`` covers every parameter EXCEPT ``mode`` and the approval
    field: the digest names WHAT would be enforced, and the enforce config
    pins that exact digest — including mode would make the pin circular.
    """

    mode: str = MODE_DISABLED
    policy_version: str = "p6-unversioned"
    fleet_min_roots: int = 30            # trigger requires root count STRICTLY above this
    d7_max_age_seconds: float = 360.0
    idle_min_minutes: float = 30.0
    strikes_required: int = 2
    strike_max_age_seconds: float = 900.0  # a strike older than this is not "the previous pass"
    max_trees_per_pass: int = 1
    max_tree_processes: int = 24
    max_tree_rss_bytes: int = 2 * 1024 ** 3
    cooldown_seconds: float = 1800.0
    approved_enforce_digest: Optional[str] = None

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "policy_version": self.policy_version,
                "fleet_min_roots": self.fleet_min_roots,
                "d7_max_age_seconds": self.d7_max_age_seconds,
                "idle_min_minutes": self.idle_min_minutes,
                "strikes_required": self.strikes_required,
                "strike_max_age_seconds": self.strike_max_age_seconds,
                "max_trees_per_pass": self.max_trees_per_pass,
                "max_tree_processes": self.max_tree_processes,
                "max_tree_rss_bytes": self.max_tree_rss_bytes,
                "cooldown_seconds": self.cooldown_seconds,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Tree/pass rejection reason codes — stable strings, asserted by tests.
REASON_INCOMPLETE_MEMBER = "incomplete_member"
REASON_INFRA_MEMBER = "infra_member"
REASON_ACTOR_MEMBER = "actor_member"
REASON_CROSS_USER_MEMBER = "cross_user_member"
REASON_DESKTOP_MEMBER = "desktop_member"
REASON_TRANSCRIPT_MISSING = "transcript_missing"
REASON_TRANSCRIPT_AMBIGUOUS = "transcript_ambiguous"
REASON_TRANSCRIPT_FUTURE = "transcript_future_mtime"
REASON_TRANSCRIPT_ACTIVE = "transcript_active"
REASON_OVERSIZE_PROCESSES = "oversize_processes"
REASON_OVERSIZE_RSS = "oversize_rss"
REASON_FIRST_STRIKE = "first_strike"
REASON_COOLDOWN_ACTIVE = "cooldown_active"
REASON_TRIGGERS_DISARMED = "triggers_disarmed"
REASON_FLEET_BELOW_MIN = "fleet_at_or_below_min"
REASON_STATE_CORRUPT = "state_corrupt"


@dataclass(frozen=True)
class TreeAssessment:
    """One whole session tree, classified. Never a partial tree."""

    root: ProcessRecord
    members: Tuple[ProcessRecord, ...]  # includes root
    total_rss: int
    transcript: TranscriptEvidence
    idle_minutes: Optional[float]
    protected: bool
    reasons: Tuple[str, ...]
    eligible: bool  # passes everything EXCEPT the strike count
    strike_key: Optional[str]


@dataclass(frozen=True)
class TargetSummary:
    """The bounded, payload-safe description of a selected tree."""

    root_identity: str
    root_pid: int
    root_create_time: float
    member_identities: Tuple[str, ...]
    member_count: int
    total_rss: int
    transcript_path: str
    transcript_mtime: float
    idle_minutes: float
    strike_key: str
    strikes: int
    action: str = ACTION_HARD_TERMINATE

    def to_payload(self) -> Dict[str, object]:
        return {
            "root_identity": self.root_identity,
            "root_pid": self.root_pid,
            "root_create_time": self.root_create_time,
            "member_identities": list(self.member_identities),
            "member_count": self.member_count,
            "total_rss": self.total_rss,
            "transcript_path": self.transcript_path,
            "transcript_mtime": self.transcript_mtime,
            "idle_minutes": round(self.idle_minutes, 1),
            "strike_key": self.strike_key,
            "strikes": self.strikes,
            "action": self.action,
        }


# Plan decisions.
DECISION_NO_ACTION = "no_action"
DECISION_SHADOW_PROJECTED = "shadow_projected"
DECISION_ENFORCE_PROJECTED = "enforce_projected"

# Result states.
RESULT_NO_ACTION = "no_action"
RESULT_SHADOW_PROJECTED = "shadow_projected"
RESULT_CANCELLED = "cancelled"
RESULT_HARD_TERMINATED = "hard_terminated"
RESULT_FAILED = "failed"


@dataclass(frozen=True)
class FleetPlan:
    schema_version: int
    policy_version: str
    policy_digest: str
    run_id: str
    mode: str
    decision: str
    triggers_armed: bool
    trigger_reasons: Tuple[str, ...]
    fleet_root_count: int
    pressure: PressureEvidence
    selected: Optional[TargetSummary]
    rejections: Tuple[Tuple[str, int], ...]  # (reason_code, count), sorted
    digest: str  # deterministic digest over policy + evidence + identities
    new_strikes: Mapping[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def plan_id(self) -> str:
        return self.digest[:16]

    def to_payload(self) -> Dict[str, object]:
        """The bounded audit payload. No cmdlines, no transcript contents."""
        return {
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "mode": self.mode,
            "decision": self.decision,
            "triggers_armed": self.triggers_armed,
            "trigger_reasons": list(self.trigger_reasons),
            "fleet_root_count": self.fleet_root_count,
            "pressure": self.pressure.to_payload(),
            "selected": self.selected.to_payload() if self.selected else None,
            "rejections": {code: count for code, count in self.rejections},
            "plan_digest": self.digest,
        }


@dataclass(frozen=True)
class FleetResult:
    run_id: str
    plan_id: str
    status: str  # RESULT_* above; "failed" is the value evaluate_outcome promotes
    executor_called: bool
    detail: str = ""
    exited_identities: Tuple[str, ...] = ()
    surviving_identities: Tuple[str, ...] = ()

    def to_payload(self) -> Dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            # Key name is load-bearing: events.outcomes scans "status", so
            # status=="failed" reaches a FAILED verdict and routing promotes
            # it TRACE->WARN with zero fleet-specific hook code.
            "status": self.status,
            "executor_called": self.executor_called,
            "detail": self.detail,
            "exited_identities": list(self.exited_identities),
            "surviving_identities": list(self.surviving_identities),
        }
