from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence

import pytest

from plugins.semantic_graph import runtime as runtime_module
from plugins.semantic_graph.cli import register_cli
from plugins.semantic_graph.config import (
    SemanticGraphConfig,
    SemanticGraphEmbeddingConfig,
)
from plugins.semantic_graph.embedding import (
    EmbeddingBackendError,
    EmbeddingModelIdentity,
    serialize_embedding_node,
    source_text_hash,
)
from plugins.semantic_graph.runtime import SemanticGraphRuntime


IDENTITY = EmbeddingModelIdentity(
    "llama.cpp", "backfill-test", "rev-b", 3, 1
)


class RecordingBackend:
    def __init__(self, *, fail: bool = False) -> None:
        self.identity = IDENTITY
        self.fail = fail
        self.calls: list[list[str]] = []

    def available(self) -> bool:
        return True

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        self.calls.append(batch)
        if self.fail:
            raise EmbeddingBackendError("injected backfill failure")
        return [[1.0, float(index + 1), 0.5] for index, _text in enumerate(batch)]


def _config() -> SemanticGraphConfig:
    return SemanticGraphConfig(
        db_subdir="semantic-graph",
        recall_statuses=("asserted", "accepted", "rejected", "superseded"),
        embedding=SemanticGraphEmbeddingConfig(
            enabled=True,
            endpoint="http://127.0.0.1:8082",
            model="backfill-test",
            revision="rev-b",
            dimensions=3,
            serializer_version=1,
            timeout_seconds=1.0,
        ),
    )


def _node(
    node_id: str,
    *,
    status: str = "asserted",
    node_type: str = "Claim",
    subtype: str = "memory.fact",
    summary: str = "Stable summary",
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": node_type,
        "subtype": subtype,
        "label": f"Label {node_id}",
        "normalized_label": f"label {node_id}".casefold(),
        "summary": summary,
        "identity_key": node_id,
        "status": status,
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": metadata or {},
    }


def _runtime(tmp_path, monkeypatch, backend: RecordingBackend) -> SemanticGraphRuntime:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        runtime_module,
        "LlamaCppEmbeddingBackend",
        lambda **_kwargs: backend,
    )
    return SemanticGraphRuntime(config=_config())


def test_backfill_dry_run_does_not_call_backend_or_mutate_db(
    tmp_path, monkeypatch
) -> None:
    backend = RecordingBackend()
    runtime = _runtime(tmp_path, monkeypatch, backend)
    runtime.store().upsert_node(_node("node-a"))
    runtime.store().upsert_node(_node("node-b"))
    before = runtime.store().get_status_counts()

    result = json.loads(
        runtime.handle_embedding_backfill({"limit": 10, "dry_run": True})
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["would_embed"] == 2
    assert result["embedded"] == 0
    assert backend.calls == []
    assert runtime.store().get_status_counts() == before
    for node_id in ("node-a", "node-b"):
        assert (
            runtime.store().get_node_embedding(
                node_id=node_id,
                namespace=IDENTITY.namespace,
            )
            is None
        )


def test_backfill_apply_uses_canonical_serializer_hash_and_namespace(
    tmp_path, monkeypatch
) -> None:
    backend = RecordingBackend()
    runtime = _runtime(tmp_path, monkeypatch, backend)
    nodes = [_node("node-a"), _node("node-b")]
    for node in nodes:
        runtime.store().upsert_node(node)

    result = json.loads(runtime.handle_embedding_backfill({"limit": 2, "apply": True}))

    assert result["success"] is True
    assert result["embedded"] == 2
    assert result["failed"] == 0
    assert result["namespace"] == IDENTITY.namespace
    assert backend.calls == [[serialize_embedding_node(node) for node in nodes]]
    for node in nodes:
        text = serialize_embedding_node(node)
        saved = runtime.store().get_node_embedding(
            node_id=str(node["node_id"]),
            namespace=IDENTITY.namespace,
        )
        assert saved is not None
        assert saved["source_text_hash"] == source_text_hash(text)


def test_backfill_excludes_noncurrent_tool_and_secret_records(
    tmp_path, monkeypatch
) -> None:
    backend = RecordingBackend()
    runtime = _runtime(tmp_path, monkeypatch, backend)
    runtime.store().upsert_node(_node("node-ok"))
    runtime.store().upsert_node(_node("node-rejected", status="rejected"))
    runtime.store().upsert_node(_node("node-superseded", status="superseded"))
    runtime.store().upsert_node(
        _node("node-tool", node_type="Artifact", subtype="tool.result")
    )
    runtime.store().upsert_node(
        _node("node-secret", summary="api_key=do-not-embed-this")
    )

    result = json.loads(
        runtime.handle_embedding_backfill({"limit": 20, "dry_run": True})
    )

    assert result["would_embed"] == 1
    assert result["excluded"] == 4
    assert backend.calls == []


def test_backfill_backend_failure_preserves_existing_vector_and_hashes_ids(
    tmp_path, monkeypatch
) -> None:
    backend = RecordingBackend(fail=True)
    runtime = _runtime(tmp_path, monkeypatch, backend)
    runtime.store().upsert_node(_node("node-private-id"))
    runtime.store().upsert_node_embedding(
        node_id="node-private-id",
        identity=IDENTITY,
        vector=[0.0, 1.0, 0.0],
        source_text_hash="a" * 64,
    )

    rendered = runtime.handle_embedding_backfill({"limit": 1, "apply": True})
    result = json.loads(rendered)

    assert result["embedded"] == 0
    assert result["failed"] == 1
    assert result["failed_node_ids_hash"] == hashlib.sha256(
        b"node-private-id"
    ).hexdigest()
    assert "node-private-id" not in rendered
    saved = runtime.store().get_node_embedding(
        node_id="node-private-id",
        namespace=IDENTITY.namespace,
    )
    assert saved is not None
    assert saved["source_text_hash"] == "a" * 64


def test_backfill_rechecks_source_hash_before_write(tmp_path, monkeypatch) -> None:
    backend = RecordingBackend()
    runtime = _runtime(tmp_path, monkeypatch, backend)
    runtime.store().upsert_node(_node("node-changing"))
    store = runtime.store()
    original_get = store.get_node

    def changed_node(node_id: str):
        row = original_get(node_id)
        assert row is not None
        return {**row, "summary": "Changed after embedding"}

    monkeypatch.setattr(store, "get_node", changed_node)

    result = json.loads(
        runtime.handle_embedding_backfill({"limit": 1, "apply": True})
    )

    assert result["embedded"] == 0
    assert result["source_changed"] == 1
    assert (
        store.get_node_embedding(
            node_id="node-changing",
            namespace=IDENTITY.namespace,
        )
        is None
    )


def test_embedding_cli_exposes_only_explicit_status_and_backfill_modes() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Runtime:
        def handle_embedding_status(self, args):
            calls.append(("status", args))
            return '{"success":true}'

        def handle_embedding_backfill(self, args):
            calls.append(("backfill", args))
            return '{"success":true}'

    class Ctx:
        def __init__(self) -> None:
            self.commands: list[dict[str, object]] = []

        def register_cli_command(self, **kwargs: object) -> None:
            self.commands.append(kwargs)

    ctx = Ctx()
    register_cli(ctx, Runtime())
    parser = argparse.ArgumentParser()
    setup = ctx.commands[0]["setup_fn"]
    handler = ctx.commands[0]["handler_fn"]
    assert callable(setup) and callable(handler)
    setup(parser)

    assert handler(parser.parse_args(["embedding-status"])) == 0
    assert handler(
        parser.parse_args(["embedding-backfill", "--limit", "7", "--dry-run"])
    ) == 0
    assert handler(
        parser.parse_args(["embedding-backfill", "--limit", "3", "--apply"])
    ) == 0
    assert calls == [
        ("status", {}),
        ("backfill", {"limit": 7, "dry_run": True, "apply": False}),
        ("backfill", {"limit": 3, "dry_run": False, "apply": True}),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(["embedding-backfill", "--limit", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["embedding-backfill", "--limit", "1", "--dry-run", "--apply"]
        )
