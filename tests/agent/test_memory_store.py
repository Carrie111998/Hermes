from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import sqlite3

import pytest

from agent.memory_records import Approval, Candidate, CanonicalRecord, Lifecycle, LifecycleStatus, MemoryClass, Provenance, ReviewPacket, Scope, TransitionReceipt
from agent.memory_store import MemoryStore, VersionConflict, _json


def scope(project: str) -> Scope:
    return Scope("profile", "project", project)


def candidate(project: str, *, source: str = "shared", cls: MemoryClass = MemoryClass.U) -> Candidate:
    return Candidate(cls, scope(project), Provenance("direct_user", source, "user"), Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"), {"value": source}, 0.8)


def record(project: str, version: int, value: str) -> CanonicalRecord:
    return CanonicalRecord("record", version, MemoryClass.U, scope(project), Provenance("direct_user", "source", "user"), Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z"), {"value": value})


def receipt(project: str, version: int) -> TransitionReceipt:
    return TransitionReceipt(f"receipt-{version}", scope(project), "record", version - 1, version, "publish", "approval")


def test_f8_candidate_filters_are_independent_and_project_isolated(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    store.append_candidate(candidate("alpha", cls=MemoryClass.C)); store.append_candidate(candidate("beta", cls=MemoryClass.C))
    assert [x.scope.project_id for x in store.query_candidates(project_id="alpha", limit=10)] == ["alpha"]
    for kwargs in ({"status": "proposed"}, {"memory_class": MemoryClass.C}, {"namespace": "project"}, {"source_id": "shared"}):
        assert len(store.query_candidates(**kwargs, limit=10)) == 2


def test_store_rejects_cross_profile_candidates(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    foreign = Candidate(
        MemoryClass.U,
        Scope("other-profile", "project", "alpha"),
        Provenance("direct_user", "source", "user"),
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"),
        {"value": "foreign"},
        0.8,
    )
    with pytest.raises(ValueError, match="does not belong"):
        store.append_candidate(foreign)


def test_f6_candidate_event_history_is_bounded(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile"); item = candidate("alpha")
    for _ in range(3): store.append_candidate(item)
    assert len(store.candidate_events(item.content_hash(), namespace="project", project_id="alpha", limit=2)) == 2
    assert store.candidate_events(item.content_hash(), namespace="project", project_id="alpha", limit=0) == []


def test_f6_concurrent_identical_review_packet_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile"); packet = ReviewPacket("packet", scope("alpha"), ("candidate",), 4)
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: store.append_review_packet(packet), range(24)))
    assert set(results) == {packet.content_hash()}
    assert store.query_review_packets(base_version=4, project_id="alpha", limit=10) == [packet]


def test_f6_transition_replay_is_sequentially_and_concurrently_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile"); rec = record("alpha", 1, "a"); proof = receipt("alpha", 1)
    store.commit_transition(rec, proof, expected_version=0); store.commit_transition(rec, proof, expected_version=0)
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: store.commit_transition(rec, proof, expected_version=0), range(24)))
    assert store.canonical_history("record", namespace="project", project_id="alpha", limit=10) == [rec]
    assert store.transition_receipts("record", namespace="project", project_id="alpha", limit=10) == [proof]


def test_f6_conflicting_transition_replay_fails_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile"); store.commit_transition(record("alpha", 1, "a"), receipt("alpha", 1), expected_version=0)
    with pytest.raises(VersionConflict):
        store.commit_transition(record("alpha", 1, "different"), receipt("alpha", 1), expected_version=0)


def test_f8_equal_receipt_ids_are_project_isolated(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    store.commit_transition(record("alpha", 1, "a"), receipt("alpha", 1), expected_version=0)
    store.commit_transition(record("beta", 1, "b"), receipt("beta", 1), expected_version=0)
    assert store.transition_receipts("record", namespace="project", project_id="alpha", limit=10) == [receipt("alpha", 1)]
    assert store.transition_receipts("record", namespace="project", project_id="beta", limit=10) == [receipt("beta", 1)]


def query_plan(store: MemoryStore, sql: str, values: tuple[object, ...]) -> str:
    with store._connect() as db:
        return " ".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, values))


def test_f9_independent_filter_queries_use_leading_indexes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    cases = [
        ("SELECT payload FROM candidates WHERE profile_id=? AND project_id=? ORDER BY candidate_id LIMIT ?", ("profile", "alpha", 10), "idx_candidates_project"),
        ("SELECT payload FROM candidates WHERE profile_id=? AND namespace=? ORDER BY candidate_id LIMIT ?", ("profile", "project", 10), "idx_candidates_namespace"),
        ("SELECT payload FROM review_packets WHERE profile_id=? AND base_version=? ORDER BY base_version,packet_id LIMIT ?", ("profile", 4, 10), "idx_review_packets_base_version"),
        ("SELECT payload FROM review_packets WHERE profile_id=? AND project_id=? ORDER BY base_version,packet_id LIMIT ?", ("profile", "alpha", 10), "idx_review_packets_project"),
    ]
    for sql, values, index in cases:
        assert index in query_plan(store, sql, values)


def test_f7_public_query_and_version_inputs_reject_malformed_values(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    for invalid in (True, -1, 501, 1.5, "1"):
        with pytest.raises((TypeError, ValueError)):
            store.query_candidates(limit=invalid)  # type: ignore[arg-type]
    for invalid in (True, -1, 1.5, "1"):
        with pytest.raises((TypeError, ValueError)):
            store.query_review_packets(base_version=invalid)  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            store.commit_transition(record("alpha", 1, "a"), receipt("alpha", 1), expected_version=invalid)  # type: ignore[arg-type]
    for kwargs in ({"project_id": " "}, {"namespace": b"project"}, {"memory_class": "U"}):
        with pytest.raises((TypeError, ValueError)):
            store.query_candidates(**kwargs)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="status"):
        store.query_candidates(status="unknown")


def test_f9_every_list_query_has_a_finite_limit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    calls = [lambda n: store.query_candidates(limit=n), lambda n: store.query_review_packets(limit=n), lambda n: store.candidate_events("id", namespace="project", project_id="alpha", limit=n), lambda n: store.canonical_history("record", namespace="project", project_id="alpha", limit=n), lambda n: store.transition_receipts("record", namespace="project", project_id="alpha", limit=n)]
    for call in calls:
        with pytest.raises(ValueError, match="limit"):
            call(501)


def test_f9_symlinked_root_state_and_database_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"; target.mkdir(); root_link = tmp_path / "root-link"; root_link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        MemoryStore(root_link, "profile")
    root = tmp_path / "root"; root.mkdir(); (root / "state").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        MemoryStore(root, "profile")
    root2 = tmp_path / "root2"; state = root2 / "state"; state.mkdir(parents=True); outside = tmp_path / "outside"; outside.write_bytes(b"sentinel"); (state / "context-memory.sqlite3").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        MemoryStore(root2, "profile")
    assert outside.read_bytes() == b"sentinel"


def test_store_revalidates_final_path_and_rejects_a_symlink_swap(tmp_path: Path) -> None:
    root = tmp_path / "root"; store = MemoryStore(root, "profile"); outside = tmp_path / "outside"; outside.write_bytes(b"sentinel")
    moved = root / "state" / "moved.sqlite3"; os.rename(store.path, moved); store.path.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store.append_candidate(candidate("alpha"))
    assert outside.read_bytes() == b"sentinel"


def test_f5_store_boundary_requires_exact_candidate_bound_class_a(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    direct_candidate = Candidate(MemoryClass.A, scope("alpha"), Provenance("direct_user", "source", "user"), Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"), {"value": "a"}, 0.8)
    direct = CanonicalRecord("record", 1, MemoryClass.A, direct_candidate.scope, direct_candidate.provenance, Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z"), direct_candidate.content)
    approval = Approval("packet", "direct-user", True)
    proof = TransitionReceipt("receipt", scope("alpha"), "record", 0, 1, "publish", approval.content_hash())
    packet = ReviewPacket("packet", scope("alpha"), (direct_candidate.content_hash(),), 0)
    derived_candidate = Candidate(MemoryClass.A, scope("alpha"), Provenance("provider", "source", "model", True), Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"), {"value": "a"}, 0.8)
    derived = CanonicalRecord("derived", 1, MemoryClass.A, derived_candidate.scope, derived_candidate.provenance, Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z"), derived_candidate.content)
    with pytest.raises(ValueError, match="direct-user non-derived"):
        store.commit_transition(derived, TransitionReceipt("derived-r", scope("alpha"), "derived", 0, 1, "publish", approval.content_hash()), expected_version=0, packet=ReviewPacket("packet", scope("alpha"), (derived_candidate.content_hash(),), 0), approval=approval, candidate=derived_candidate)
    bad_packets = (
        ReviewPacket("packet", scope("alpha"), (direct_candidate.content_hash(), "other"), 0),
        ReviewPacket("packet", scope("alpha"), (direct_candidate.content_hash(),), 1),
        ReviewPacket("packet", scope("alpha"), ("not-the-candidate",), 0),
    )
    for bad_packet in bad_packets:
        with pytest.raises(ValueError, match="candidate-bound"):
            store.commit_transition(direct, proof, expected_version=0, packet=bad_packet, approval=approval, candidate=direct_candidate)
    changed = CanonicalRecord("record", 1, MemoryClass.A, direct_candidate.scope, direct_candidate.provenance, Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z"), {"value": "changed"})
    with pytest.raises(ValueError, match="candidate-bound"):
        store.commit_transition(changed, proof, expected_version=0, packet=packet, approval=approval, candidate=direct_candidate)
    with pytest.raises(ValueError, match="candidate-bound"):
        store.commit_transition(direct, proof, expected_version=0, packet=packet, approval=approval)
    store.commit_transition(direct, proof, expected_version=0, packet=packet, approval=approval, candidate=direct_candidate)


def class_a_transition(
    tmp_path: Path,
    *,
    candidate_observed_at: str = "2026-01-01T00:00:00Z",
    record_observed_at: str = "2026-01-01T00:00:00Z",
    candidate_supersedes: str | None = None,
    record_supersedes: str | None = None,
) -> tuple[MemoryStore, CanonicalRecord, TransitionReceipt, ReviewPacket, Approval, Candidate]:
    item = Candidate(
        MemoryClass.A,
        scope("alpha"),
        Provenance("direct_user", "source", "user"),
        Lifecycle(
            LifecycleStatus.PROPOSED,
            observed_at=candidate_observed_at,
            supersedes_record_id=candidate_supersedes,
        ),
        {"value": "approved"},
        0.8,
    )
    canonical = CanonicalRecord(
        "record",
        1,
        MemoryClass.A,
        item.scope,
        item.provenance,
        Lifecycle(
            LifecycleStatus.ACTIVE,
            observed_at=record_observed_at,
            effective_at="2026-02-02T00:00:00Z",
            supersedes_record_id=record_supersedes,
        ),
        item.content,
    )
    packet = ReviewPacket("packet", item.scope, (item.content_hash(),), 0)
    approval = Approval("packet", "direct-user", True)
    proof = TransitionReceipt("receipt", item.scope, "record", 0, 1, "publish", approval.content_hash())
    return MemoryStore(tmp_path, "profile"), canonical, proof, packet, approval, item


def test_class_a_publication_rejects_unapproved_observed_at(tmp_path: Path) -> None:
    store, canonical, proof, packet, approval, item = class_a_transition(
        tmp_path,
        record_observed_at="2026-02-01T00:00:00Z",
    )

    with pytest.raises(ValueError, match="candidate-bound"):
        store.commit_transition(canonical, proof, expected_version=0, packet=packet, approval=approval, candidate=item)

    assert store.canonical_history("record", namespace="project", project_id="alpha") == []


def test_class_a_publication_rejects_different_supersession_target(tmp_path: Path) -> None:
    store, canonical, proof, packet, approval, item = class_a_transition(
        tmp_path,
        candidate_supersedes="approved-target",
        record_supersedes="different-target",
    )

    with pytest.raises(ValueError, match="candidate-bound"):
        store.commit_transition(canonical, proof, expected_version=0, packet=packet, approval=approval, candidate=item)

    assert store.canonical_history("record", namespace="project", project_id="alpha") == []


def test_class_a_initial_publication_rejects_unapproved_supersession(tmp_path: Path) -> None:
    store, canonical, proof, packet, approval, item = class_a_transition(
        tmp_path,
        candidate_supersedes=None,
        record_supersedes="unapproved-target",
    )

    with pytest.raises(ValueError, match="candidate-bound"):
        store.commit_transition(canonical, proof, expected_version=0, packet=packet, approval=approval, candidate=item)

    assert store.canonical_history("record", namespace="project", project_id="alpha") == []


def test_f6_historical_and_concurrent_transition_replay_is_idempotent(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    first = record("alpha", 1, "one"); first_receipt = receipt("alpha", 1)
    second = record("alpha", 2, "two"); second_receipt = receipt("alpha", 2)
    store.commit_transition(first, first_receipt, expected_version=0)
    store.commit_transition(second, second_receipt, expected_version=1)
    store.commit_transition(first, first_receipt, expected_version=0)
    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(lambda _: store.commit_transition(first, first_receipt, expected_version=0), range(16)))
    conflicting_receipt = TransitionReceipt("receipt-1-other", scope("alpha"), "record", 0, 1, "publish", "approval")
    with pytest.raises(VersionConflict):
        store.commit_transition(first, conflicting_receipt, expected_version=0)
    with pytest.raises(VersionConflict):
        store.commit_transition(record("alpha", 1, "changed"), first_receipt, expected_version=0)
    def conflicting(_: int) -> type[BaseException] | None:
        try:
            store.commit_transition(record("alpha", 1, "changed"), first_receipt, expected_version=0)
        except BaseException as exc:
            return type(exc)
        return None
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert set(workers.map(conflicting, range(16))) == {VersionConflict}


def test_f6_conflicting_review_packet_replay_fails(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    store.append_review_packet(ReviewPacket("same", scope("alpha"), ("one",), 0))
    with pytest.raises(ValueError, match="different content"):
        store.append_review_packet(ReviewPacket("same", scope("alpha"), ("two",), 0))


def test_f9_every_independent_list_filter_has_an_index_plan(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    cases = [
        ("SELECT payload FROM candidates WHERE profile_id=? AND status=? ORDER BY candidate_id LIMIT ?", ("profile", "proposed", 10), "idx_candidates_status"),
        ("SELECT payload FROM candidates WHERE profile_id=? AND memory_class=? ORDER BY candidate_id LIMIT ?", ("profile", "U", 10), "idx_candidates_class"),
        ("SELECT payload FROM candidates WHERE profile_id=? AND source_id=? ORDER BY candidate_id LIMIT ?", ("profile", "source", 10), "idx_candidates_source"),
        ("SELECT payload FROM review_packets WHERE profile_id=? AND namespace=? ORDER BY base_version,packet_id LIMIT ?", ("profile", "project", 10), "idx_review_packets_namespace"),
        ("SELECT event FROM candidate_events WHERE profile_id=? AND namespace=? AND project_id=? AND candidate_id=? ORDER BY event_sequence LIMIT ?", ("profile", "project", "alpha", "candidate", 10), "idx_candidate_events_lookup"),
        ("SELECT payload FROM canonical_records WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=? ORDER BY version LIMIT ?", ("profile", "project", "alpha", "record", 10), "idx_canonical_versions"),
        ("SELECT payload FROM transition_receipts WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=? ORDER BY to_version LIMIT ?", ("profile", "project", "alpha", "record", 10), "idx_receipts_record"),
    ]
    for sql, values, index in cases:
        assert index in query_plan(store, sql, values)


def test_f7_all_public_query_parameters_reject_wrong_shapes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    calls = [
        lambda: store.query_candidates(status=MemoryClass.U),  # type: ignore[arg-type]
        lambda: store.query_candidates(memory_class="U"),  # type: ignore[arg-type]
        lambda: store.query_candidates(project_id=" "),
        lambda: store.query_review_packets(base_version=True),
        lambda: store.candidate_events("candidate", namespace="project", project_id="alpha", limit=1.5),  # type: ignore[arg-type]
        lambda: store.canonical_history("record", namespace="project", project_id="alpha", min_version=False),
        lambda: store.transition_receipts("record", namespace="project", project_id="alpha", limit="10"),  # type: ignore[arg-type]
    ]
    for call in calls:
        with pytest.raises((TypeError, ValueError)):
            call()


def test_f9_hardlinks_nonregular_and_non_sqlite_files_are_rejected(tmp_path: Path) -> None:
    for kind in ("hardlink", "fifo", "not-sqlite"):
        root = tmp_path / kind; state = root / "state"; state.mkdir(parents=True)
        database = state / "context-memory.sqlite3"
        if kind == "hardlink":
            source = tmp_path / "source.sqlite3"; source.touch(); os.link(source, database)
        elif kind == "fifo":
            os.mkfifo(database)
        else:
            database.write_bytes(b"not a sqlite database")
        with pytest.raises(ValueError):
            MemoryStore(root, "profile")


def test_store_has_no_identity_sidecar_and_uses_delete_journal(tmp_path: Path) -> None:
    root = tmp_path / "root"; store = MemoryStore(root, "profile")
    assert not (root / "state" / "context-memory.identity").exists()
    with store._connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_transition_fault_rolls_back_record_and_receipt_together(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    with store._connect() as db:
        db.execute(
            "CREATE TRIGGER reject_receipt BEFORE INSERT ON transition_receipts "
            "BEGIN SELECT RAISE(ABORT, 'injected fault'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected fault"):
        store.commit_transition(record("alpha", 1, "one"), receipt("alpha", 1), expected_version=0)
    assert store.canonical_history("record", namespace="project", project_id="alpha") == []
    assert store.transition_receipts("record", namespace="project", project_id="alpha") == []



def test_f6_receipt_only_conflict_and_concurrent_conflicting_packets_fail_closed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    first = record("alpha", 1, "one")
    store.commit_transition(first, receipt("alpha", 1), expected_version=0)
    conflicting_receipt = TransitionReceipt("different-receipt", scope("alpha"), "record", 0, 1, "publish", "approval")
    with pytest.raises(VersionConflict):
        store.commit_transition(first, conflicting_receipt, expected_version=0)

    left = ReviewPacket("shared-id", scope("alpha"), ("left",), 0)
    right = ReviewPacket("shared-id", scope("alpha"), ("right",), 0)
    def append_conflict(index: int) -> tuple[str, str]:
        packet = left if index % 2 == 0 else right
        try:
            return ("ok", store.append_review_packet(packet))
        except ValueError:
            return ("conflict", packet.content_hash())
    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(append_conflict, range(32)))
    assert {kind for kind, _ in outcomes} == {"ok", "conflict"}
    persisted = store.review_packet("shared-id", namespace="project", project_id="alpha")
    assert persisted in (left, right)
    assert {value for kind, value in outcomes if kind == "ok"} == {persisted.content_hash()}


def test_f10_intermediate_path_attacks_fail_closed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"; outside.mkdir()
    intermediate = tmp_path / "intermediate"; intermediate.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        MemoryStore(intermediate / "child", "profile")
    blocked = tmp_path / "blocked"; blocked.write_text("not a directory")
    with pytest.raises((ValueError, OSError)):
        MemoryStore(blocked / "child", "profile")




def test_f10_canonical_history_min_max_and_range_use_canonical_index(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, "profile")
    base = "SELECT payload FROM canonical_records WHERE profile_id=? AND namespace=? AND project_id=? AND record_id=?"
    cases = [
        (base + " AND version>=? ORDER BY version LIMIT ?", ("profile", "project", "alpha", "record", 1, 10)),
        (base + " AND version<=? ORDER BY version LIMIT ?", ("profile", "project", "alpha", "record", 2, 10)),
        (base + " AND version>=? AND version<=? ORDER BY version LIMIT ?", ("profile", "project", "alpha", "record", 1, 2, 10)),
    ]
    for sql, values in cases:
        assert "idx_canonical_versions" in query_plan(store, sql, values)


def test_f10_storage_serializer_rejects_nonfinite_values_directly() -> None:
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            _json({"value": value})
