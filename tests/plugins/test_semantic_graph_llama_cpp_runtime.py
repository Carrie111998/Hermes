from __future__ import annotations

import json

from plugins.semantic_graph import runtime as runtime_module
from plugins.semantic_graph.config import (
    SemanticGraphConfig,
    SemanticGraphEmbeddingConfig,
)
from plugins.semantic_graph.embedding import (
    DeterministicFakeEmbeddingBackend,
    EmbeddingModelIdentity,
    serialize_embedding_node,
    serialize_embedding_query,
    source_text_hash,
)
from plugins.semantic_graph.retrieval import render_context, search_and_rank
from plugins.semantic_graph.runtime import SemanticGraphRuntime


def _config(*, enabled: bool) -> SemanticGraphConfig:
    return SemanticGraphConfig(
        db_subdir="semantic-graph",
        min_recall_confidence=0.0,
        embedding=SemanticGraphEmbeddingConfig(
            enabled=enabled,
            endpoint="http://127.0.0.1:8082",
            model="runtime-test",
            revision="rev-a",
            dimensions=3,
            serializer_version=1,
            timeout_seconds=1.5,
        ),
    )


def _node(node_id: str, label: str, summary: str) -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Preference",
        "subtype": "runtime-test",
        "label": label,
        "normalized_label": label.casefold(),
        "summary": summary,
        "identity_key": node_id,
        "status": "asserted",
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": {},
    }


def _state(runtime: SemanticGraphRuntime) -> list[tuple[object, ...]]:
    return [
        (
            row["node_id"],
            row["status"],
            row["authority"],
            row["confidence"],
            row["salience"],
            row["updated_at"],
        )
        for row in runtime.store().list_nodes(limit=100)
    ]


def _stable_results(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {key: value for key, value in row.items() if key != "final_score"}
        for row in rows
    ]


def test_disabled_embedding_preserves_lexical_results_context_and_state(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class UnexpectedBackend:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("disabled embedding must not construct a backend")

    monkeypatch.setattr(
        runtime_module,
        "LlamaCppEmbeddingBackend",
        UnexpectedBackend,
        raising=False,
    )
    runtime = SemanticGraphRuntime(config=_config(enabled=False))
    runtime.store().upsert_node(
        _node("node-python", "frontend Python", "User prefers Python for frontend")
    )

    expected = search_and_rank(
        runtime.store(),
        "frontend Python",
        top_k=8,
        min_confidence=0.0,
        statuses=["asserted", "accepted"],
    )
    expected_context = render_context(expected, runtime.config.retrieval_max_chars)
    before = _state(runtime)

    result = json.loads(
        runtime.handle_search({"query": "frontend Python", "top_k": 8})
    )
    context = runtime.on_pre_llm_call(user_message="frontend Python")
    status = json.loads(runtime.handle_embedding_status({}))

    assert _stable_results(result["results"]) == _stable_results(expected)
    assert context == ({"context": expected_context} if expected_context else None)
    assert status["enabled"] is False
    assert _state(runtime) == before


def test_enabled_runtime_constructs_only_the_configured_llama_cpp_backend(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = EmbeddingModelIdentity(
        "llama.cpp", "runtime-test", "rev-a", 3, 1
    )
    backend = DeterministicFakeEmbeddingBackend(identity=identity, vectors={})
    constructed: list[dict[str, object]] = []

    def build_backend(**kwargs: object) -> DeterministicFakeEmbeddingBackend:
        constructed.append(kwargs)
        return backend

    monkeypatch.setattr(runtime_module, "LlamaCppEmbeddingBackend", build_backend)
    runtime = SemanticGraphRuntime(config=_config(enabled=True))

    first = json.loads(runtime.handle_embedding_status({}))
    second = json.loads(runtime.handle_embedding_status({}))

    assert first == second
    assert first["success"] is True
    assert first["enabled"] is True
    assert first["available"] is True
    assert first["namespace"] == identity.namespace
    assert constructed == [
        {
            "endpoint": "http://127.0.0.1:8082",
            "model": "runtime-test",
            "revision": "rev-a",
            "dimensions": 3,
            "serializer_version": 1,
            "timeout_seconds": 1.5,
            "allow_remote": False,
        }
    ]


def test_enabled_runtime_uses_existing_hybrid_retrieval_without_mutation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = EmbeddingModelIdentity(
        "llama.cpp", "runtime-test", "rev-a", 3, 1
    )
    query = "frontend stack"
    nodes = [
        _node("node-dense", "backend choice", "User prefers the dense candidate"),
        _node("node-lexical", "frontend stack", "Lexical candidate"),
    ]
    vectors = {serialize_embedding_query(query): [1.0, 0.0, 0.0]}
    for index, node in enumerate(nodes):
        vectors[serialize_embedding_node(node)] = (
            [1.0, 0.0, 0.0] if index == 0 else [0.0, 1.0, 0.0]
        )
    backend = DeterministicFakeEmbeddingBackend(identity=identity, vectors=vectors)
    monkeypatch.setattr(
        runtime_module,
        "LlamaCppEmbeddingBackend",
        lambda **_kwargs: backend,
    )
    runtime = SemanticGraphRuntime(config=_config(enabled=True))
    for node in nodes:
        runtime.store().upsert_node(node)
        text = serialize_embedding_node(node)
        runtime.store().upsert_node_embedding(
            node_id=str(node["node_id"]),
            identity=identity,
            vector=vectors[text],
            source_text_hash=source_text_hash(text),
        )
    before = _state(runtime)

    result = json.loads(runtime.handle_search({"query": query, "top_k": 8}))

    assert result["success"] is True
    assert result["results"]
    assert any(row.get("dense_rank") is not None for row in result["results"])
    assert _state(runtime) == before


def test_embedding_failure_returns_exact_lexical_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    identity = EmbeddingModelIdentity(
        "llama.cpp", "runtime-test", "rev-a", 3, 1
    )
    backend = DeterministicFakeEmbeddingBackend(
        identity=identity,
        vectors={},
        fail_on_embed=True,
    )
    monkeypatch.setattr(
        runtime_module,
        "LlamaCppEmbeddingBackend",
        lambda **_kwargs: backend,
    )
    runtime = SemanticGraphRuntime(config=_config(enabled=True))
    runtime.store().upsert_node(
        _node("node-python", "frontend Python", "User prefers Python for frontend")
    )
    expected = search_and_rank(
        runtime.store(),
        "frontend Python",
        top_k=8,
        min_confidence=0.0,
        statuses=["asserted", "accepted"],
    )

    result = json.loads(
        runtime.handle_search({"query": "frontend Python", "top_k": 8})
    )

    assert _stable_results(result["results"]) == _stable_results(expected)
