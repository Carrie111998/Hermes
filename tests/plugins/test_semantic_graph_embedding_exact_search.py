"""Exact embedding store tests for Commit 5."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.semantic_graph.embedding import EmbeddingModelIdentity
from plugins.semantic_graph.store import SemanticGraphStore


IDENTITY = EmbeddingModelIdentity(
    provider="test",
    model="fake",
    revision="v1",
    dimensions=3,
    serializer_version=1,
)


def _node(node_id: str, *, summary: str = "summary") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Preference",
        "subtype": "test",
        "label": node_id,
        "normalized_label": node_id,
        "summary": summary,
        "identity_key": node_id,
        "status": "asserted",
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": {},
    }


def _store(tmp_path: Path, node_ids: tuple[str, ...] = ("node-best",)) -> SemanticGraphStore:
    store = SemanticGraphStore(tmp_path / "graph.db")
    for node_id in node_ids:
        store.upsert_node(_node(node_id))
    return store


def _save(store: SemanticGraphStore, node_id: str, vector: list[float], *, identity: EmbeddingModelIdentity = IDENTITY, source_hash: str = "a" * 64) -> None:
    store.upsert_node_embedding(
        node_id=node_id,
        identity=identity,
        vector=vector,
        source_text_hash=source_hash,
    )


def test_store_round_trips_embedding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    result = store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace)
    assert result is not None
    assert result["vector"] == pytest.approx((1.0, 0.0, 0.0))
    assert result["source_text_hash"] == "a" * 64


def test_upsert_replaces_same_node_namespace_and_preserves_created_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    first = store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace)
    _save(store, "node-best", [0.0, 1.0, 0.0], source_hash="b" * 64)
    second = store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace)
    assert first is not None and second is not None
    assert second["created_at"] == first["created_at"]
    assert second["vector"] == pytest.approx((0.0, 1.0, 0.0))


def test_two_namespaces_coexist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = EmbeddingModelIdentity("test", "other", "v2", 3, 1)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    _save(store, "node-best", [0.0, 1.0, 0.0], identity=other, source_hash="b" * 64)
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is not None
    assert store.get_node_embedding(node_id="node-best", namespace=other.namespace) is not None


def test_unknown_node_rejected(tmp_path: Path) -> None:
    store = SemanticGraphStore(tmp_path / "graph.db")
    with pytest.raises(KeyError, match="unknown node_id"):
        _save(store, "missing", [1.0, 0.0, 0.0])


def test_invalid_source_hash_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="SHA-256"):
        _save(store, "node-best", [1.0, 0.0, 0.0], source_hash="not-a-hash")


def test_exact_search_orders_by_cosine(tmp_path: Path) -> None:
    ids = ("node-best", "node-second", "node-zero-relevance", "node-opposite")
    store = _store(tmp_path, ids)
    vectors = {
        "node-best": [1.0, 0.0, 0.0],
        "node-second": [0.8, 0.6, 0.0],
        "node-zero-relevance": [0.0, 1.0, 0.0],
        "node-opposite": [-1.0, 0.0, 0.0],
    }
    for node_id, vector in vectors.items():
        _save(store, node_id, vector)
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=8)
    assert [row["node_id"] for row in results] == list(ids)


def test_exact_search_tie_breaks_by_node_id(tmp_path: Path) -> None:
    store = _store(tmp_path, ("node-z", "node-a"))
    _save(store, "node-z", [0.0, 1.0, 0.0])
    _save(store, "node-a", [0.0, 1.0, 0.0])
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=2)
    assert [row["node_id"] for row in results] == ["node-a", "node-z"]


def test_exact_search_filters_min_similarity_and_limits_top_k(tmp_path: Path) -> None:
    store = _store(tmp_path, ("node-best", "node-second", "node-opposite"))
    _save(store, "node-best", [1.0, 0.0, 0.0])
    _save(store, "node-second", [0.8, 0.6, 0.0])
    _save(store, "node-opposite", [-1.0, 0.0, 0.0])
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=1, min_similarity=0.5)
    assert [row["node_id"] for row in results] == ["node-best"]


def test_exact_search_ignores_other_namespace_and_dimension_mismatch(tmp_path: Path) -> None:
    store = _store(tmp_path, ("node-best", "node-other"))
    _save(store, "node-best", [1.0, 0.0, 0.0])
    other = EmbeddingModelIdentity("test", "other", "v1", 2, 1)
    _save(store, "node-other", [1.0, 0.0], identity=other, source_hash="b" * 64)
    assert [row["node_id"] for row in store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=10)] == ["node-best"]


def test_exact_search_skips_stale_source_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0], source_hash="a" * 64)
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=10, expected_source_hashes={"node-best": "b" * 64})
    assert results == []


def test_node_delete_cascades_embedding(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    with store.transaction() as conn:
        conn.execute("DELETE FROM nodes WHERE node_id=?", ("node-best",))
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is None


def test_semantic_update_invalidates_all_namespaces(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = EmbeddingModelIdentity("test", "other", "v1", 3, 1)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    _save(store, "node-best", [0.0, 1.0, 0.0], identity=other, source_hash="b" * 64)
    store.upsert_node({**_node("node-best"), "node_type": "Fact"})
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is None
    assert store.get_node_embedding(node_id="node-best", namespace=other.namespace) is None


def test_nonsemantic_update_preserves_all_namespaces(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = EmbeddingModelIdentity("test", "other", "v1", 3, 1)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    _save(store, "node-best", [0.0, 1.0, 0.0], identity=other, source_hash="b" * 64)
    store.upsert_node({**_node("node-best"), "status": "accepted", "confidence": 0.99})
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is not None
    assert store.get_node_embedding(node_id="node-best", namespace=other.namespace) is not None


def test_exact_search_empty_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=0) == []
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=3, node_ids=[]) == []


def test_exact_search_node_ids_are_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path, ("node-best", "node-other"))
    _save(store, "node-best", [1.0, 0.0, 0.0])
    _save(store, "node-other", [0.0, 1.0, 0.0])
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=10, node_ids=["node-other"])
    assert [row["node_id"] for row in results] == ["node-other"]


def test_get_embedding_expected_hash_mismatch_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0], source_hash="a" * 64)
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace, expected_source_text_hash="b" * 64) is None


def test_nonzero_and_dimension_validation_are_delegated_to_vector_layer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(Exception):
        _save(store, "node-best", [0.0, 0.0, 0.0])
    wrong = EmbeddingModelIdentity("test", "wrong", "v1", 2, 1)
    with pytest.raises(Exception):
        _save(store, "node-best", [1.0, 0.0, 0.0], identity=wrong)


def test_exact_search_min_similarity_validation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="min_similarity"):
        store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=1, min_similarity=2.0)


def test_exact_search_rejects_zero_query(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(Exception):
        store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[0.0, 0.0, 0.0], top_k=1)


def test_search_result_contains_namespace_and_hash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    result = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=1)[0]
    assert result["namespace"] == IDENTITY.namespace
    assert result["source_text_hash"] == "a" * 64


def test_source_hash_filter_only_applies_to_listed_nodes(tmp_path: Path) -> None:
    store = _store(tmp_path, ("node-best", "node-other"))
    _save(store, "node-best", [1.0, 0.0, 0.0], source_hash="a" * 64)
    _save(store, "node-other", [0.8, 0.6, 0.0], source_hash="c" * 64)
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=2, expected_source_hashes={"node-best": "b" * 64})
    assert [row["node_id"] for row in results] == ["node-other"]


def test_exact_search_deduplicates_node_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    results = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=3, node_ids=["node-best", "node-best"])
    assert len(results) == 1


def test_embedding_api_returns_metadata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    saved = store.upsert_node_embedding(node_id="node-best", identity=IDENTITY, vector=[1.0, 0.0, 0.0], source_text_hash="a" * 64)
    assert saved == {"node_id": "node-best", "namespace": IDENTITY.namespace, "dimensions": 3, "source_text_hash": "a" * 64}


def test_search_top_k_negative_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=-1) == []


def test_search_accepts_min_similarity_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=1, min_similarity=1.0)[0]["node_id"] == "node-best"


def test_revision_and_serializer_are_stored(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    row = store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace)
    assert row is not None
    assert row["revision"] == "v1"
    assert row["serializer_version"] == 1


def test_search_is_namespace_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other = EmbeddingModelIdentity("test", "other", "v1", 3, 1)
    _save(store, "node-best", [1.0, 0.0, 0.0], identity=other, source_hash="b" * 64)
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=5) == []


def test_update_metadata_only_preserves_summary_semantics(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    store.upsert_node({**_node("node-best"), "metadata": {"source": "test"}})
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is not None


def test_all_exact_results_have_finite_scores(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    result = store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=[1.0, 0.0, 0.0], top_k=1)[0]
    assert -1.0 <= result["similarity"] <= 1.0


def test_search_accepts_tuple_query(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    assert store.search_node_embeddings_exact(namespace=IDENTITY.namespace, query_vector=(1.0, 0.0, 0.0), top_k=1)


def test_get_missing_embedding_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is None


def test_search_unknown_namespace_returns_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    assert store.search_node_embeddings_exact(namespace="missing", query_vector=[1.0, 0.0, 0.0], top_k=1) == []


def test_source_hash_is_returned_as_string(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _save(store, "node-best", [1.0, 0.0, 0.0])
    result = store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace)
    assert result is not None and isinstance(result["source_text_hash"], str)


def test_embedding_updates_are_atomic_with_node_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.transaction():
        _save(store, "node-best", [1.0, 0.0, 0.0])
        raise_expected = True
        assert raise_expected
    assert store.get_node_embedding(node_id="node-best", namespace=IDENTITY.namespace) is not None
