from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.semantic_graph_embedding_ab_benchmark import (
    BENCHMARK_IDENTITY,
    NAMESPACE,
    HttpEmbeddingClient,
    _eligible,
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
