from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.semantic_graph_embedding_ab_benchmark import (
    BENCHMARK_IDENTITY,
    NAMESPACE,
    HttpEmbeddingClient,
    _eligible,
    _query_observation,
    _candidate_observations,
    load_fixture,
    make_store,
    run_variant,
    serialize_bge_query,
)
from plugins.semantic_graph.embedding import serialize_embedding_node, source_text_hash


FIXTURE = Path(__file__).parents[1] / "fixtures" / "semantic_graph_retrieval_benchmark.json"


def test_fixture_and_raw_bge_profile_are_fixed() -> None:
    fixture = load_fixture()
    assert len(fixture["queries"]) == 90
    assert "Instruct:" not in serialize_bge_query("What language does the user prefer?")
    assert "Query:" not in serialize_bge_query("What language does the user prefer?")
    assert NAMESPACE == BENCHMARK_IDENTITY.namespace
    assert NAMESPACE.startswith("benchmark:")


def test_lexical_variant_reuses_baseline_contract_without_mutation(tmp_path: Path) -> None:
    fixture = load_fixture()
    store, run_a, _run_b = make_store(tmp_path / "benchmark.db")
    before = {
        row["node_id"]: (row["status"], row["authority"], row["confidence"])
        for row in store.list_nodes(limit=5000)
    }

    summary = run_variant(store, fixture, run_a, dense=False, client=None)

    after = {
        row["node_id"]: (row["status"], row["authority"], row["confidence"])
        for row in store.list_nodes(limit=5000)
    }
    assert summary["query_count"] == 90
    assert summary["groups"]["japanese_to_english"]["recall_at_8"] == 1.0
    assert summary["groups"]["correction_history"]["recall_at_8"] == 1.0
    assert summary["state_mutation_count"] == 0
    assert before == after


def test_dense_candidate_boundary_and_stale_hash(tmp_path: Path) -> None:
    fixture = load_fixture()
    store, run_a, _run_b = make_store(tmp_path / "benchmark.db")
    eligible = _eligible(store, run_a, fixture["queries"][0])
    assert eligible
    assert all(row["status"] in {"asserted", "accepted"} for row in eligible)
    assert all(float(row["confidence"]) >= 0.60 for row in eligible)
    assert "run-b-only" not in {row["node_id"] for row in eligible}

    row = eligible[0]
    document = serialize_embedding_node(row)
    vector = [0.0] * 1024
    vector[0] = 1.0
    store.upsert_node_embedding(
        node_id=row["node_id"],
        identity=BENCHMARK_IDENTITY,
        vector=vector,
        source_text_hash=source_text_hash(document),
    )
    assert store.search_node_embeddings_exact(
        namespace=NAMESPACE,
        query_vector=vector,
        top_k=8,
        node_ids=[row["node_id"]],
        expected_source_hashes={row["node_id"]: "0" * 64},
    ) == []


def test_live_execution_is_explicit(tmp_path: Path) -> None:
    from scripts.semantic_graph_embedding_ab_benchmark import run_benchmark

    with pytest.raises(RuntimeError, match="--live"):
        run_benchmark(
            base_url="http://127.0.0.1:8084",
            model="nsfw-bge-m3-v4.gguf",
            db_path=tmp_path / "benchmark.db",
            live=False,
            output=tmp_path / "result.json",
        )


def test_http_client_validates_dimension_and_finite_values(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({
                "data": [{"index": 0, "embedding": [1.0] * 1024}],
            }).encode()

    monkeypatch.setattr(
        "scripts.semantic_graph_embedding_ab_benchmark.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )
    client = HttpEmbeddingClient("http://127.0.0.1:8084")
    assert len(client.embed_query("raw query")) == 1024


def test_candidate_observation_preserves_full_fused_ranks() -> None:
    from plugins.semantic_graph.fusion import reciprocal_rank_fusion

    fused = reciprocal_rank_fusion(
        lexical_ids=["lexical-only", "shared", "expected"],
        dense_ids=["shared", "expected", "dense-only"],
        dense_similarities={"shared": 0.91, "expected": 0.73, "dense-only": 0.52},
    )

    observed = _candidate_observations(
        fused,
        lexical_ids=["lexical-only", "shared", "expected"],
        dense_ids=["shared", "expected", "dense-only"],
        top_k=2,
    )

    assert observed[0]["node_id"] == "shared"
    assert observed[0]["lexical_rank"] == 2
    assert observed[0]["dense_rank"] == 1
    assert observed[0]["dense_similarity"] == 0.91
    assert observed[0]["source_count"] == 2
    assert observed[0]["best_rank"] == 1
    assert observed[0]["final_rank"] == 1
    assert observed[0]["selected_into_top8"] is True

    dense_only = next(row for row in observed if row["node_id"] == "dense-only")
    assert dense_only["final_rank"] > 2
    assert dense_only["selected_into_top8"] is False


def test_query_observation_handles_negative_and_positive_queries() -> None:
    positive = _query_observation(
        expected=["expected"],
        lexical_ids=["expected", "other"],
        dense_ids=["expected", "other"],
        dense_similarities={"expected": 0.8, "other": 0.4},
        fused_node_ids=["expected", "other"],
        top_k=8,
    )
    negative = _query_observation(
        expected=[],
        lexical_ids=["other"],
        dense_ids=["other"],
        dense_similarities={"other": 0.4},
        fused_node_ids=["other"],
        top_k=8,
    )

    assert positive["top1_dense_similarity"] == 0.8
    assert positive["top2_dense_similarity"] == 0.4
    assert positive["dense_top_margin"] == 0.4
    assert positive["top1_rrf_score"] > positive["top2_rrf_score"]
    assert positive["lexical_dense_top1_agreement"] is True
    assert positive["lexical_dense_expected_overlap"] == 1
    assert negative["lexical_dense_expected_overlap"] == 0
    assert negative["lexical_dense_top1_agreement"] is True


def test_run_variant_emits_observation_schema_for_lexical_variant(tmp_path: Path) -> None:
    fixture = load_fixture()
    store, run_a, _run_b = make_store(tmp_path / "benchmark.db")

    summary = run_variant(store, fixture, run_a, dense=False, client=None)
    result = summary["query_results"][0]

    assert result["candidates"]
    assert set(result["candidates"][0]) >= {
        "node_id", "lexical_rank", "dense_rank", "dense_similarity", "rrf_score",
        "source_count", "best_rank", "final_rank", "selected_into_top8",
        "cognitive_shadow",
    }
    assert set(result["candidates"][0]["cognitive_shadow"]) == {
        "base_rank",
        "memory_link_count",
        "representative_memory_id",
        "projected_retention",
        "access_state",
        "belief_status",
        "cognitive_score",
        "would_filter",
        "cognitive_rank",
        "rank_changed",
        "reason",
    }
    assert summary["cognitive_shadow_observation_count"] > 0
    assert summary["state_mutation_count"] == 0
    assert set(result["observation"]) >= {
        "top1_dense_similarity", "top2_dense_similarity", "dense_top_margin",
        "top1_rrf_score", "top2_rrf_score", "rrf_top_margin",
        "lexical_dense_top1_agreement", "lexical_dense_expected_overlap",
    }


def test_benchmark_results_persist_fixture_ids_not_query_text(tmp_path: Path) -> None:
    fixture = load_fixture()
    store, run_a, _run_b = make_store(tmp_path / "benchmark.db")

    summary = run_variant(store, fixture, run_a, dense=False, client=None)

    results = summary["query_results"]
    assert len(results) == 90
    assert [row["fixture_id"] for row in results] == [
        f"q-{index:03d}" for index in range(1, 91)
    ]
    assert all("query" not in row for row in results)
