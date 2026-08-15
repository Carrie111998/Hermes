"""Evidence-vector classification with preserving unknown semantics."""

from __future__ import annotations

from dataclasses import asdict
from typing import Iterable, Mapping

from .types import (
    CoverageState,
    EvidenceVector,
    MotionState,
    ProcessState,
    ReclaimDecision,
    TriState,
)


def classify_evidence(
    observations: Iterable[Mapping[str, object]],
    *,
    now: int,
    required_idle_windows: int = 2,
) -> EvidenceVector:
    """Classify complete evidence without converting missing data to absence."""

    items = list(observations)
    ids = tuple(str(item.get("observation_id", "")) for item in items if item.get("observation_id"))
    reasons: list[str] = []
    if not items:
        return EvidenceVector(
            process=ProcessState.UNKNOWN,
            motion=MotionState.UNKNOWN,
            artifacts=TriState.UNKNOWN,
            publication=TriState.UNKNOWN,
            coverage=CoverageState.UNKNOWN,
            decision=ReclaimDecision.UNKNOWN,
            reason_codes=("no_observations",),
            observation_ids=(),
        )

    if any(int(item.get("fresh_until", 0)) < now for item in items):
        reasons.append("stale_observation")
    if any(item.get("complete") is False for item in items):
        reasons.append("incomplete_source")
    if any(item.get("access_denied") is True for item in items):
        reasons.append("access_denied")
    if any(item.get("generation_match") is False for item in items):
        reasons.append("generation_mismatch")
    if any(item.get("cursor_gap") is True or item.get("spool_unhealthy") is True for item in items):
        reasons.append("event_gap")

    coverage_values = {str(item.get("coverage", "unknown")) for item in items}
    if coverage_values == {"strong"}:
        coverage = CoverageState.STRONG
    elif "unknown" in coverage_values or reasons:
        coverage = CoverageState.UNKNOWN
    else:
        coverage = CoverageState.BEST_EFFORT

    process_values = {str(item.get("process", "unknown")) for item in items}
    if process_values == {"dead"}:
        process = ProcessState.DEAD
    elif "alive" in process_values:
        process = ProcessState.ALIVE
    else:
        process = ProcessState.UNKNOWN

    # Heartbeat is intentionally excluded from worker motion.  Observer-owned
    # samples also never count as motion by themselves.
    worker_motion = [
        bool(item.get("worker_motion"))
        for item in items
        if str(item.get("source", "")) != "observer" and not bool(item.get("heartbeat_only"))
    ]
    idle_windows = sum(1 for item in items if item.get("idle_window_complete") is True)
    if any(worker_motion):
        motion = MotionState.ACTIVE
    elif idle_windows >= required_idle_windows and not reasons:
        motion = MotionState.IDLE
    else:
        motion = MotionState.UNKNOWN

    artifact_values = {str(item.get("artifacts", "unknown")) for item in items}
    if "present" in artifact_values:
        artifacts = TriState.PRESENT
    elif artifact_values == {"absent"} and not reasons:
        artifacts = TriState.ABSENT
    else:
        artifacts = TriState.UNKNOWN

    publication_values = {str(item.get("publication", "unknown")) for item in items}
    if "present" in publication_values:
        publication = TriState.PRESENT
    elif publication_values == {"absent"} and not reasons:
        publication = TriState.ABSENT
    else:
        publication = TriState.UNKNOWN

    preserving = (
        bool(reasons)
        or artifacts is not TriState.ABSENT
        or publication is not TriState.ABSENT
        or coverage is not CoverageState.STRONG
    )
    if preserving:
        decision = ReclaimDecision.PRESERVE if artifacts is TriState.PRESENT or publication is TriState.PRESENT else ReclaimDecision.UNKNOWN
    elif process is ProcessState.DEAD:
        decision = ReclaimDecision.ELIGIBLE_DEAD
    elif process is ProcessState.ALIVE and motion is MotionState.IDLE:
        freeze_supported = all(bool(item.get("freeze_supported")) for item in items)
        if freeze_supported:
            decision = ReclaimDecision.ELIGIBLE_INERT
        else:
            reasons.append("freeze_unsupported")
            decision = ReclaimDecision.PRESERVE
    else:
        decision = ReclaimDecision.PRESERVE

    return EvidenceVector(
        process=process,
        motion=motion,
        artifacts=artifacts,
        publication=publication,
        coverage=coverage,
        decision=decision,
        reason_codes=tuple(sorted(set(reasons))),
        observation_ids=ids,
    )


def evidence_json(vector: EvidenceVector) -> dict[str, object]:
    value = asdict(vector)
    for key in ("process", "motion", "artifacts", "publication", "coverage", "decision"):
        value[key] = getattr(vector, key).value
    value["reason_codes"] = list(vector.reason_codes)
    value["observation_ids"] = list(vector.observation_ids)
    return value
