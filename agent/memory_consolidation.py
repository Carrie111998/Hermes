from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from agent.memory_policy import validate_candidate
from agent.memory_records import Candidate, CanonicalRecord, LifecycleStatus, MemoryClass, Scope


MAX_CANDIDATES = 100


@dataclass(frozen=True)
class EvaluationFinding:
    code: str
    candidate_id: str | None = None


@dataclass(frozen=True)
class ConsolidationProposal:
    scope: Scope
    memory_class: MemoryClass
    candidate_ids: tuple[str, ...]
    content: Mapping[str, Any]
    supersedes_record_id: str | None
    expected_canonical_version: int
    evaluation_findings: tuple[EvaluationFinding, ...] = ()
    approved: bool = field(default=False, init=False)
    authoritative: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ConsolidationResult:
    proposal: ConsolidationProposal | None
    findings: tuple[EvaluationFinding, ...]


def _failed(*findings: EvaluationFinding) -> ConsolidationResult:
    return ConsolidationResult(None, findings)


def consolidate_candidates(
    candidate_ids: tuple[str, ...],
    candidates: tuple[Candidate, ...],
    *,
    expected_canonical_version: int,
    active: CanonicalRecord | None = None,
) -> ConsolidationResult:
    if (
        type(candidate_ids) is not tuple
        or not candidate_ids
        or len(candidate_ids) > MAX_CANDIDATES
        or any(type(candidate_id) is not str or not candidate_id.strip() for candidate_id in candidate_ids)
        or len(set(candidate_ids)) != len(candidate_ids)
    ):
        return _failed(EvaluationFinding("invalid_candidate_ids"))
    if (
        type(candidates) is not tuple
        or not candidates
        or len(candidates) != len(candidate_ids)
        or any(type(candidate) is not Candidate for candidate in candidates)
    ):
        return _failed(EvaluationFinding("invalid_candidates"))
    if type(expected_canonical_version) is not int or expected_canonical_version < 0:
        return _failed(EvaluationFinding("invalid_expected_canonical_version"))
    if active is not None and type(active) is not CanonicalRecord:
        return _failed(EvaluationFinding("invalid_active_record"))

    by_id = {candidate.content_hash(): candidate for candidate in candidates}
    if len(by_id) != len(candidates):
        return _failed(EvaluationFinding("duplicate_candidate"))
    if set(candidate_ids) != set(by_id):
        return _failed(EvaluationFinding("candidate_id_mismatch"))

    ordered_ids = tuple(sorted(candidate_ids))
    ordered = tuple(by_id[candidate_id] for candidate_id in ordered_ids)
    first = ordered[0]
    findings: list[EvaluationFinding] = []

    for candidate_id, candidate in zip(ordered_ids, ordered):
        findings.extend(EvaluationFinding(code, candidate_id) for code in validate_candidate(candidate))
    if any(candidate.scope != first.scope for candidate in ordered[1:]):
        findings.append(EvaluationFinding("candidate_scope_mismatch"))
    if any(candidate.memory_class is not first.memory_class for candidate in ordered[1:]):
        findings.append(EvaluationFinding("candidate_memory_class_mismatch"))
    if any(candidate.content != first.content for candidate in ordered[1:]):
        findings.append(EvaluationFinding("candidate_content_mismatch"))
    target = first.lifecycle.supersedes_record_id
    if any(candidate.lifecycle.supersedes_record_id != target for candidate in ordered[1:]):
        findings.append(EvaluationFinding("candidate_supersession_mismatch"))

    if active is None:
        if expected_canonical_version != 0:
            findings.append(EvaluationFinding("initial_version_must_be_zero"))
        if target is not None:
            findings.append(EvaluationFinding("unexpected_supersession_target"))
    else:
        if active.lifecycle.status is not LifecycleStatus.ACTIVE:
            findings.append(EvaluationFinding("active_record_must_be_active"))
        if first.scope != active.scope:
            findings.append(EvaluationFinding("active_scope_mismatch"))
        if first.memory_class is not active.memory_class:
            findings.append(EvaluationFinding("active_memory_class_mismatch"))
        if expected_canonical_version != active.version:
            findings.append(EvaluationFinding("active_version_mismatch"))
        if target != active.record_id:
            findings.append(EvaluationFinding("active_supersession_target_mismatch"))

    if findings:
        return ConsolidationResult(None, tuple(findings))
    proposal = ConsolidationProposal(
        scope=first.scope,
        memory_class=first.memory_class,
        candidate_ids=ordered_ids,
        content=first.content,
        supersedes_record_id=target,
        expected_canonical_version=expected_canonical_version,
    )
    return ConsolidationResult(proposal, ())
