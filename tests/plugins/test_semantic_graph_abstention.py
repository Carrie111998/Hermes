from __future__ import annotations

import json
from collections import Counter

import pytest

from plugins.semantic_graph.abstention import (
    decide_abstention,
    extract_retrieval_features,
)
from plugins.semantic_graph.config import (
    SemanticGraphCognitiveMemoryConfig,
    SemanticGraphConfig,
)
from plugins.semantic_graph.runtime import SemanticGraphRuntime
from scripts.semantic_graph_embedding_ab_benchmark import (
    build_cognitive_evaluation_fixture,
    load_cognitive_fixture,
    load_fixture,
    select_evaluation_fixture,
)


def _candidate(
    node_id: str,
    *,
    lexical_rank: int | None,
    dense_rank: int | None,
    dense_similarity: float | None,
    rrf_score: float,
    source_count: int,
    retention: float | None = 0.8,
    access_state: str | None = "accessible",
    belief_status: str | None = "current",
    cognitive_rank: int = 1,
    would_filter: bool = False,
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "lexical_rank": lexical_rank,
        "dense_rank": dense_rank,
        "dense_similarity": dense_similarity,
        "rrf_score": rrf_score,
        "source_count": source_count,
        "cognitive_shadow": {
            "projected_retention": retention,
            "access_state": access_state,
            "belief_status": belief_status,
            "cognitive_rank": cognitive_rank,
            "would_filter": would_filter,
        },
    }


def test_cognitive_fixture_expansion_counts_and_split_are_deterministic() -> None:
    base = load_fixture()
    spec = load_cognitive_fixture()
    first = build_cognitive_evaluation_fixture(base, spec)
    second = build_cognitive_evaluation_fixture(base, spec)

    assert first == second
    assert len(base["queries"]) == 90
    assert len(first["queries"]) == 240
    counts = Counter(query["group"] for query in first["queries"])
    assert counts["negative_no_memory"] == 50
    assert counts["temporal_update"] == 20
    assert counts["contradiction"] == 20
    assert counts["cue_trigger_disconnect"] == 20
    assert counts["tool_action_grounding"] == 20
    assert counts["memory_poisoning_repair"] == 20
    assert {query["split"] for query in first["queries"]} == {
        "development",
        "holdout",
    }
    for group in (
        "negative_no_memory",
        "temporal_update",
        "contradiction",
        "cue_trigger_disconnect",
        "tool_action_grounding",
        "memory_poisoning_repair",
    ):
        assert {
            query["split"]
            for query in first["queries"]
            if query["group"] == group
        } == {"development", "holdout"}


def test_evaluation_split_selection_keeps_holdout_disjoint() -> None:
    baseline = select_evaluation_fixture("baseline")
    development = select_evaluation_fixture("development")
    holdout = select_evaluation_fixture("holdout")
    complete = select_evaluation_fixture("all")

    baseline_ids = {query.get("fixture_id") for query in baseline["queries"]}
    development_ids = {query["fixture_id"] for query in development["queries"]}
    holdout_ids = {query["fixture_id"] for query in holdout["queries"]}
    complete_ids = {query["fixture_id"] for query in complete["queries"]}
    assert len(baseline_ids) == 1  # legacy fixture intentionally has no IDs
    assert development_ids
    assert holdout_ids
    assert development_ids.isdisjoint(holdout_ids)
    assert development_ids | holdout_ids == complete_ids
    assert len(complete_ids) == 240


def test_feature_extraction_and_gate_are_small_deterministic_and_query_free() -> None:
    strong = [
        _candidate(
            "strong",
            lexical_rank=1,
            dense_rank=1,
            dense_similarity=0.88,
            rrf_score=2.0 / 61.0,
            source_count=2,
        ),
        _candidate(
            "other",
            lexical_rank=2,
            dense_rank=2,
            dense_similarity=0.40,
            rrf_score=2.0 / 62.0,
            source_count=2,
            cognitive_rank=2,
        ),
    ]
    features = extract_retrieval_features(strong, query_length=37)
    decision = decide_abstention(features)

    assert set(features) == {
        "top1_dense_similarity",
        "top2_dense_similarity",
        "dense_margin",
        "top1_rrf_score",
        "rrf_margin",
        "lexical_dense_top1_agreement",
        "source_count",
        "candidate_count",
        "projected_retention",
        "current_ratio",
        "latent_ratio",
        "noncurrent_ratio",
        "query_length",
    }
    assert features["dense_margin"] == pytest.approx(0.48)
    assert features["lexical_dense_top1_agreement"] is True
    assert features["candidate_count"] == 2
    assert features["query_length"] == 37
    assert decision["abstain"] is False
    assert "query" not in features
    assert "query" not in decision

    weak = extract_retrieval_features(
        [
            _candidate(
                "weak-a",
                lexical_rank=1,
                dense_rank=2,
                dense_similarity=0.20,
                rrf_score=1.0 / 61.0,
                source_count=1,
            ),
            _candidate(
                "weak-b",
                lexical_rank=2,
                dense_rank=1,
                dense_similarity=0.19,
                rrf_score=1.0 / 61.0,
                source_count=1,
                cognitive_rank=2,
            ),
        ],
        query_length=12,
    )
    assert decide_abstention(weak)["abstain"] is True

    dense_unavailable = extract_retrieval_features(
        [
            _candidate(
                "lexical-only",
                lexical_rank=1,
                dense_rank=None,
                dense_similarity=None,
                rrf_score=1.0 / 61.0,
                source_count=1,
            )
        ],
        query_length=10,
    )
    assert decide_abstention(dense_unavailable) == {
        "abstain": False,
        "reason": "dense_unavailable_fail_open",
    }


def test_measured_development_gate_uses_three_bounded_evidence_routes() -> None:
    base = {
        "top2_dense_similarity": 0.40,
        "top1_rrf_score": 0.02,
        "lexical_dense_top1_agreement": False,
        "source_count": 2,
        "candidate_count": 8,
        "projected_retention": 0.8,
        "current_ratio": 1.0,
        "latent_ratio": 0.0,
        "noncurrent_ratio": 0.0,
        "query_length": 20,
    }
    strong_dense = {
        **base,
        "top1_dense_similarity": 0.51,
        "dense_margin": 0.01,
        "rrf_margin": 0.0001,
    }
    high_rrf_margin = {
        **base,
        "top1_dense_similarity": 0.19,
        "dense_margin": 0.01,
        "rrf_margin": 0.02,
    }
    ambiguous_midrange = {
        **base,
        "top1_dense_similarity": 0.45,
        "dense_margin": 0.05,
        "rrf_margin": 0.005,
    }

    assert decide_abstention(strong_dense)["abstain"] is False
    assert decide_abstention(high_rrf_margin)["abstain"] is False
    assert decide_abstention(ambiguous_midrange)["abstain"] is True


def _node(node_id: str, label: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Claim",
        "subtype": "memory.fact",
        "label": label,
        "normalized_label": label.casefold(),
        "summary": label,
        "identity_key": node_id,
        "status": "asserted",
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": {},
    }


def _link_state(
    runtime: SemanticGraphRuntime,
    *,
    node_id: str,
    memory_id: int,
    access_state: str,
    belief_status: str = "current",
) -> None:
    runtime.store().upsert_memory_node_link(
        {
            "memory_id": memory_id,
            "node_id": node_id,
            "belief_id": f"belief-{memory_id}",
            "belief_version": 1,
            "relation": "represents",
        }
    )
    runtime.store().upsert_memory_state_cache(
        {
            "memory_id": memory_id,
            "belief_id": f"belief-{memory_id}",
            "belief_version": 1,
            "access_state": access_state,
            "belief_status": belief_status,
            "memory_state": "active",
            "retention_at_sync": 0.9,
            "stability_days": 3650.0,
            "salience": 0.8,
            "valence": 0.0,
            "confidence": 0.9,
            "protected": False,
            "source_updated_at": 100.0,
            "synced_at": 101.0,
        }
    )


def test_active_runtime_filters_then_reranks_without_mutation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = SemanticGraphRuntime(
        config=SemanticGraphConfig(
            min_recall_confidence=0.0,
            cognitive_memory=SemanticGraphCognitiveMemoryConfig(
                rerank_enabled=True,
                mode="active",
                abstention_enabled=False,
            ),
        )
    )
    runtime.store().upsert_node(_node("latent", "Python latent"))
    runtime.store().upsert_node(_node("current", "Python current"))
    _link_state(runtime, node_id="latent", memory_id=1, access_state="latent")
    _link_state(runtime, node_id="current", memory_id=2, access_state="accessible")

    hits = [
        {**_node("latent", "Python latent"), **_candidate(
            "latent",
            lexical_rank=1,
            dense_rank=1,
            dense_similarity=0.9,
            rrf_score=2.0 / 61.0,
            source_count=2,
        )},
        {**_node("current", "Python current"), **_candidate(
            "current",
            lexical_rank=2,
            dense_rank=2,
            dense_similarity=0.8,
            rrf_score=2.0 / 62.0,
            source_count=2,
            cognitive_rank=2,
        )},
    ]
    monkeypatch.setattr(
        "plugins.semantic_graph.runtime.hybrid_search_and_rank",
        lambda *args, **kwargs: [dict(row) for row in hits],
    )
    before = runtime.store().get_status_counts()

    result = json.loads(runtime.handle_search({"query": "Python"}))

    assert [row["node_id"] for row in result["results"]] == ["current"]
    assert runtime.store().get_status_counts() == before


def test_active_abstention_blocks_weak_dense_but_backend_absence_fails_open(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = SemanticGraphRuntime(
        config=SemanticGraphConfig(
            min_recall_confidence=0.0,
            cognitive_memory=SemanticGraphCognitiveMemoryConfig(
                rerank_enabled=True,
                mode="active",
                abstention_enabled=True,
            ),
        )
    )
    runtime.store().upsert_node(_node("weak", "Unrelated memory"))
    weak = {
        **_node("weak", "Unrelated memory"),
        **_candidate(
            "weak",
            lexical_rank=1,
            dense_rank=1,
            dense_similarity=0.1,
            rrf_score=2.0 / 61.0,
            source_count=2,
        ),
    }
    monkeypatch.setattr(
        "plugins.semantic_graph.runtime.hybrid_search_and_rank",
        lambda *args, **kwargs: [dict(weak)],
    )
    assert json.loads(runtime.handle_search({"query": "weather"}))["results"] == []

    lexical_only = dict(weak)
    lexical_only.update(dense_rank=None, dense_similarity=None, source_count=1)
    monkeypatch.setattr(
        "plugins.semantic_graph.runtime.hybrid_search_and_rank",
        lambda *args, **kwargs: [dict(lexical_only)],
    )
    fail_open = json.loads(runtime.handle_search({"query": "weather"}))
    assert [row["node_id"] for row in fail_open["results"]] == ["weak"]
