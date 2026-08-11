"""Commit 6 lexical/dense RRF contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.semantic_graph.embedding import (
    DeterministicFakeEmbeddingBackend,
    EmbeddingBackendError,
    EmbeddingModelIdentity,
    serialize_embedding_node,
    serialize_embedding_query,
    source_text_hash,
)
from plugins.semantic_graph.fusion import RetrievalCandidate, reciprocal_rank_fusion
from plugins.semantic_graph.retrieval import hybrid_search_and_rank
from plugins.semantic_graph.store import SemanticGraphStore


IDENTITY = EmbeddingModelIdentity("fake", "commit6", "v1", 3, 1)


def test_rrf_merges_sources_and_calculates_one_based_scores() -> None:
    rows = reciprocal_rank_fusion(
        lexical_ids=["lexical", "both"],
        dense_ids=["both", "dense"],
    )

    assert rows[0].node_id == "both"
    assert rows[0].lexical_rank == 2
    assert rows[0].dense_rank == 1
    assert rows[0].source_count == 2
    assert rows[0].rrf_score == pytest.approx(1 / 62 + 1 / 61)
    assert rows[0].best_rank == 1
    assert rows[-1].node_id == "dense"


def test_rrf_deduplicates_each_source_using_first_rank() -> None:
    rows = reciprocal_rank_fusion(
        lexical_ids=["a", "a", "b"],
        dense_ids=["b", "b", "a"],
    )

    assert [row.node_id for row in rows] == ["a", "b"]
    assert rows[0].lexical_rank == 1
    assert rows[0].dense_rank == 3
    assert rows[1].lexical_rank == 3
    assert rows[1].dense_rank == 1


def test_rrf_ties_use_source_count_then_best_rank_then_node_id() -> None:
    rows = reciprocal_rank_fusion(
        lexical_ids=["a", "b", "c", "d"],
        dense_ids=["d", "c", "b", "a"],
        k=0,
    )

    # a/d have two sources and equal scores; best rank breaks the tie.
    assert [row.node_id for row in rows] == ["a", "d", "b", "c"]
    assert rows[0].best_rank == 1
    assert rows[1].best_rank == 1


def test_rrf_empty_and_single_source_inputs_are_supported() -> None:
    assert reciprocal_rank_fusion(lexical_ids=[], dense_ids=[]) == []
    assert [row.node_id for row in reciprocal_rank_fusion(lexical_ids=["b", "a"], dense_ids=[])] == ["b", "a"]
    assert [row.node_id for row in reciprocal_rank_fusion(lexical_ids=[], dense_ids=["b", "a"])] == ["b", "a"]


def _store(tmp_path: Path) -> SemanticGraphStore:
    store = SemanticGraphStore(tmp_path / "semantic.db")
    store.ensure_ready()
    return store


def _node(node_id: str, summary: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Preference",
        "subtype": "test",
        "label": node_id,
        "normalized_label": node_id.casefold(),
        "summary": summary,
        "identity_key": node_id,
        "status": "asserted",
        "authority": "user",
        "confidence": 0.95,
        "salience": 0.8,
    }


def _seed_embedding(
    store: SemanticGraphStore,
    node: dict[str, object],
    vector: list[float],
    *,
    source_hash: str | None = None,
) -> None:
    text = serialize_embedding_node(node)
    store.upsert_node_embedding(
        node_id=str(node["node_id"]),
        identity=IDENTITY,
        vector=vector,
        source_text_hash=source_hash or source_text_hash(text),
    )


def _backend(query: str, *, fail: bool = False) -> DeterministicFakeEmbeddingBackend:
    return DeterministicFakeEmbeddingBackend(
        identity=IDENTITY,
        vectors={query: [1.0, 0.0, 0.0]},
        fail_on_embed=fail,
    )


def test_hybrid_search_keeps_lexical_and_dense_only_nodes_and_caps_top_k(tmp_path: Path) -> None:
    store = _store(tmp_path)
    lexical = _node("lexical", "alpha lexical phrase")
    both = _node("both", "alpha shared phrase")
    dense = _node("dense", "unrelated dense phrase")
    for node in (lexical, both, dense):
        store.upsert_node(node)
    _seed_embedding(store, both, [1.0, 0.0, 0.0])
    _seed_embedding(store, dense, [0.99, 0.01, 0.0])

    query = "alpha lexical phrase"
    backend = _backend(serialize_embedding_query(query))
    rows = hybrid_search_and_rank(store, query, backend=backend, top_k=3)

    assert len(rows) == 3
    assert {row["node_id"] for row in rows} == {"lexical", "both", "dense"}
    assert rows[0]["node_id"] == "both"
    assert rows[0]["lexical_rank"] is not None
    assert rows[0]["dense_rank"] == 1
    assert rows[0]["dense_similarity"] == pytest.approx(1.0)


def test_hybrid_dense_failure_preserves_lexical_order_exactly(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for node in (_node("a", "alpha one"), _node("b", "alpha two")):
        store.upsert_node(node)
    query = "alpha"
    lexical = hybrid_search_and_rank(store, query, backend=None, top_k=8)
    failed = hybrid_search_and_rank(store, query, backend=_backend(serialize_embedding_query(query), fail=True), top_k=8)
    assert [row["node_id"] for row in failed] == [row["node_id"] for row in lexical]


def test_hybrid_disabled_empty_dense_and_backend_error_fallback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    node = _node("a", "alpha")
    store.upsert_node(node)
    query = "alpha"
    lexical = hybrid_search_and_rank(store, query, backend=None)
    assert [row["node_id"] for row in hybrid_search_and_rank(store, query, embedding_enabled=False)] == [row["node_id"] for row in lexical]
    empty = DeterministicFakeEmbeddingBackend(identity=IDENTITY, vectors={})
    assert [row["node_id"] for row in hybrid_search_and_rank(store, query, backend=empty)] == [row["node_id"] for row in lexical]


def test_hybrid_is_read_only_and_excludes_stale_embeddings(tmp_path: Path) -> None:
    store = _store(tmp_path)
    node = _node("a", "alpha")
    stale = _node("stale", "unrelated stale content")
    store.upsert_node(node)
    store.upsert_node(stale)
    _seed_embedding(store, stale, [1.0, 0.0, 0.0], source_hash="f" * 64)
    before = {item["node_id"]: (item["status"], item["authority"]) for item in store.list_nodes(limit=10)}
    query = "alpha"
    backend = _backend(serialize_embedding_query(query))
    rows = hybrid_search_and_rank(store, query, backend=backend)
    after = {item["node_id"]: (item["status"], item["authority"]) for item in store.list_nodes(limit=10)}
    assert before == after
    assert all(row["node_id"] != "stale" for row in rows)


def test_rrf_candidate_is_frozen() -> None:
    candidate = RetrievalCandidate("x", 1, None, None, 1.0, 1)
    with pytest.raises(AttributeError):
        candidate.node_id = "y"  # type: ignore[misc]


def test_backend_error_type_remains_narrow() -> None:
    assert issubclass(EmbeddingBackendError, RuntimeError)
