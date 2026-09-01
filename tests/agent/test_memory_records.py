from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import replace
import math
from typing import Any, cast

import pytest

from agent.memory_records import Approval, Candidate, CanonicalRecord, Lifecycle, LifecycleStatus, MemoryClass, Provenance, ReviewPacket, Scope, TransitionReceipt, _digest


def scoped(project: str = "alpha") -> Scope:
    return Scope("profile", "project", project)


def proposed(**changes: object) -> Candidate:
    original = Candidate(MemoryClass.U, scoped(), Provenance("direct_user", "message", "user"), Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"), {"policy": {"enabled": True}}, 0.5)
    return replace(original, **changes)


def test_f7_scope_and_packet_text_are_nonblank() -> None:
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            Scope("profile", "project", bad)
        with pytest.raises(ValueError, match="candidate_ids"):
            ReviewPacket("packet", scoped(), (bad,), 0)


def test_f7_candidate_ids_reject_wrong_containers_and_versions() -> None:
    for invalid in ("one", b"one", ["one"]):
        with pytest.raises((TypeError, ValueError)):
            ReviewPacket("packet", scoped(), invalid, 0)  # type: ignore[arg-type]
    for invalid in (True, -1, 1.0, "1"):
        with pytest.raises((TypeError, ValueError)):
            ReviewPacket("packet", scoped(), ("one",), invalid)  # type: ignore[arg-type]


def test_f7_mapping_content_is_required_and_deeply_frozen() -> None:
    with pytest.raises(TypeError, match="mapping"):
        proposed(content=["not", "mapping"])
    source = {"nested": {"values": [1]}}
    item = proposed(content=source); digest = item.content_hash(); source["nested"]["values"].append(2)
    assert item.content_hash() == digest
    assert item.to_dict()["content"] == {"nested": {"values": [1]}}
    with pytest.raises(TypeError):
        cast(MutableMapping[str, Any], item.content)["new"] = 1


@pytest.mark.parametrize("content", [{"": 1}, {"key": "   "}, {"key": math.nan}, {"key": math.inf}, {"key": -math.inf}])
def test_f7_blank_and_nonfinite_content_is_rejected(content: object) -> None:
    with pytest.raises((TypeError, ValueError), match="content"):
        proposed(content=content)


def test_runtime_memory_class_is_exact() -> None:
    with pytest.raises(TypeError, match="memory_class"):
        proposed(memory_class="A")  # type: ignore[arg-type]
    assert [entry.value for entry in MemoryClass] == ["U", "A", "E", "P", "T", "R", "C"]


def test_f8_transition_receipt_identity_includes_full_scope() -> None:
    alpha = TransitionReceipt("receipt", scoped("alpha"), "record", 0, 1, "publish", "approval")
    beta = TransitionReceipt("receipt", scoped("beta"), "record", 0, 1, "publish", "approval")
    assert alpha.content_hash() != beta.content_hash()
    assert alpha.to_dict()["scope"] == scoped("alpha").to_dict()


def test_all_transition_records_have_stable_distinct_hashes() -> None:
    item = proposed(); packet = ReviewPacket("packet", scoped(), (item.content_hash(),), 0); approval = Approval("packet", "direct-user", True)
    record = CanonicalRecord("record", 1, item.memory_class, item.scope, item.provenance, Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-01T00:00:00Z", effective_at="2026-01-02T00:00:00Z"), item.content)
    receipt = TransitionReceipt("receipt", scoped(), "record", 0, 1, "publish", approval.content_hash())
    assert len({packet.content_hash(), approval.content_hash(), record.content_hash(), receipt.content_hash()}) == 4


def test_f10_digest_boundary_rejects_nonfinite_values_directly() -> None:
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            _digest({"value": value})


def test_lifecycle_states_are_exact_and_unknown_values_fail_closed() -> None:
    assert [state.value for state in LifecycleStatus] == [
        "proposed", "active", "disputed", "superseded", "expired", "rejected"
    ]
    with pytest.raises(ValueError, match="status"):
        Lifecycle("unknown", observed_at="2026-01-01T00:00:00Z")  # type: ignore[arg-type]


def test_lifecycle_timestamps_and_links_reject_inconsistent_values() -> None:
    with pytest.raises(ValueError, match="observed_at"):
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="not-a-timestamp")
    with pytest.raises(ValueError, match="effective_at"):
        Lifecycle(LifecycleStatus.ACTIVE, observed_at="2026-01-02T00:00:00Z")
    with pytest.raises(ValueError, match="effective_at"):
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-02T00:00:00Z", effective_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="expires_at"):
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-02T00:00:00Z", expires_at="2026-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="supersedes_record_id"):
        Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-02T00:00:00Z", supersedes_record_id=" ")
    with pytest.raises(ValueError, match="expires_at"):
        Lifecycle(
            LifecycleStatus.EXPIRED,
            observed_at="2026-01-01T00:00:00Z",
            effective_at="2026-01-03T00:00:00Z",
        )
    with pytest.raises(ValueError, match="effective_at"):
        Lifecycle(
            LifecycleStatus.EXPIRED,
            observed_at="2026-01-01T00:00:00Z",
            effective_at="2026-01-02T00:00:00Z",
            expires_at="2026-01-03T00:00:00Z",
        )


def test_lifecycle_identity_includes_time_and_supersession() -> None:
    first = proposed(lifecycle=Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-01T00:00:00Z"))
    later = proposed(lifecycle=Lifecycle(LifecycleStatus.PROPOSED, observed_at="2026-01-02T00:00:00Z"))
    replacement = proposed(lifecycle=Lifecycle(
        LifecycleStatus.PROPOSED,
        observed_at="2026-01-01T00:00:00Z",
        supersedes_record_id="record-1",
    ))
    assert len({first.content_hash(), later.content_hash(), replacement.content_hash()}) == 3


def test_provenance_rejects_unknown_source_and_actor_shapes() -> None:
    with pytest.raises(ValueError, match="source_type"):
        Provenance("unknown", "source", "user")
    with pytest.raises(ValueError, match="actor"):
        Provenance("direct_user", "source", " ")
    with pytest.raises(ValueError, match="actor"):
        Provenance("direct_user", "source", "intruder")
    with pytest.raises(ValueError, match="derived"):
        Provenance("direct_user", "source", "user", True)
    with pytest.raises(ValueError, match="derived"):
        Provenance("provider", "source", "provider", False)
