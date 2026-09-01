from __future__ import annotations

from dataclasses import replace
import importlib

from agent.memory_records import Candidate, CanonicalRecord, Lifecycle, LifecycleStatus, MemoryClass, Provenance, Scope


def module():
    return importlib.import_module("agent.memory_consolidation")


def candidate(*, project: str = "alpha", source: str = "source", value: str = "same", memory_class: MemoryClass = MemoryClass.U, supersedes: str | None = None) -> Candidate:
    return Candidate(
        memory_class,
        Scope("profile", "project", project),
        Provenance("direct_user", source, "user"),
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z", supersedes_record_id=supersedes),
        {"value": value},
        0.8,
    )


def active(*, version: int = 3) -> CanonicalRecord:
    item = candidate()
    return CanonicalRecord(
        "record-1",
        version,
        item.memory_class,
        item.scope,
        item.provenance,
        Lifecycle(LifecycleStatus.ACTIVE, observed_at=item.lifecycle.observed_at, effective_at="2026-01-02T00:00:00Z"),
        {"value": "old"},
    )


def codes(result) -> tuple[str, ...]:
    return tuple(finding.code for finding in result.findings)


def test_consolidation_is_order_independent_and_inert() -> None:
    api = module()
    first = candidate(source="one")
    second = candidate(source="two")
    ids = (first.content_hash(), second.content_hash())

    left = api.consolidate_candidates(tuple(reversed(ids)), (second, first), expected_canonical_version=0)
    right = api.consolidate_candidates(ids, (first, second), expected_canonical_version=0)

    assert left == right
    assert left.findings == ()
    assert left.proposal is not None
    assert left.proposal.candidate_ids == tuple(sorted(ids))
    assert dict(left.proposal.content) == {"value": "same"}
    assert left.proposal.approved is False
    assert left.proposal.authoritative is False
    assert left.proposal.evaluation_findings == ()
    assert first.lifecycle.status is LifecycleStatus.PROPOSED
    assert second.lifecycle.status is LifecycleStatus.PROPOSED


def test_candidate_ids_are_nonempty_unique_and_bounded() -> None:
    api = module()
    item = candidate()
    identity = item.content_hash()

    assert codes(api.consolidate_candidates((), (), expected_canonical_version=0)) == ("invalid_candidate_ids",)
    assert codes(api.consolidate_candidates((identity, identity), (item, item), expected_canonical_version=0)) == ("invalid_candidate_ids",)
    too_many = tuple(f"candidate-{index}" for index in range(api.MAX_CANDIDATES + 1))
    assert codes(api.consolidate_candidates(too_many, (), expected_canonical_version=0)) == ("invalid_candidate_ids",)


def test_candidate_ids_exactly_match_candidate_content_hashes() -> None:
    api = module()
    item = candidate()
    result = api.consolidate_candidates(("wrong-id",), (item,), expected_canonical_version=0)
    assert codes(result) == ("candidate_id_mismatch",)
    assert result.proposal is None


def test_mixed_scope_fails_closed() -> None:
    api = module()
    alpha = candidate(project="alpha", source="one")
    beta = candidate(project="beta", source="two")
    result = api.consolidate_candidates((alpha.content_hash(), beta.content_hash()), (alpha, beta), expected_canonical_version=0)
    assert codes(result) == ("candidate_scope_mismatch",)
    assert result.proposal is None


def test_mixed_memory_class_and_content_fail_closed_in_stable_order() -> None:
    api = module()
    first = candidate(source="one", value="first")
    second = candidate(source="two", value="second", memory_class=MemoryClass.C)
    result = api.consolidate_candidates((second.content_hash(), first.content_hash()), (second, first), expected_canonical_version=0)
    assert codes(result) == ("candidate_memory_class_mismatch", "candidate_content_mismatch")
    assert result.proposal is None


def test_initial_proposal_requires_version_zero_and_no_supersession() -> None:
    api = module()
    item = candidate(supersedes="record-1")
    result = api.consolidate_candidates((item.content_hash(),), (item,), expected_canonical_version=2)
    assert codes(result) == ("initial_version_must_be_zero", "unexpected_supersession_target")
    assert result.proposal is None


def test_supersession_proposal_carries_exact_active_precondition() -> None:
    api = module()
    current = active(version=3)
    first = candidate(source="one", value="new", supersedes=current.record_id)
    second = candidate(source="two", value="new", supersedes=current.record_id)
    result = api.consolidate_candidates((second.content_hash(), first.content_hash()), (second, first), expected_canonical_version=3, active=current)

    assert result.findings == ()
    assert result.proposal is not None
    assert result.proposal.supersedes_record_id == current.record_id
    assert result.proposal.expected_canonical_version == current.version
    assert result.proposal.scope == current.scope
    assert result.proposal.memory_class is current.memory_class


def test_stale_or_wrong_active_state_fails_closed() -> None:
    api = module()
    current = active(version=3)
    item = candidate(value="new", supersedes=current.record_id)
    stale = api.consolidate_candidates((item.content_hash(),), (item,), expected_canonical_version=2, active=current)
    assert codes(stale) == ("active_version_mismatch",)

    disputed = replace(current, lifecycle=replace(current.lifecycle, status=LifecycleStatus.DISPUTED))
    wrong_state = api.consolidate_candidates((item.content_hash(),), (item,), expected_canonical_version=3, active=disputed)
    assert codes(wrong_state) == ("active_record_must_be_active",)
