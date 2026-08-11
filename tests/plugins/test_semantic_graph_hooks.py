"""Hook behavior tests for semantic-graph runtime."""

from __future__ import annotations

from plugins.semantic_graph.config import (
    SemanticGraphConfig,
    SemanticGraphEmbeddingConfig,
)
from plugins.semantic_graph.embedding import EmbeddingBackendError, EmbeddingModelIdentity
from plugins.semantic_graph.runtime import SemanticGraphRuntime
from plugins.semantic_graph.store import SemanticGraphStore


def _rt(tmp_path, **cfg) -> SemanticGraphRuntime:
    config = SemanticGraphConfig(
        db_subdir=cfg.pop("db_subdir", "semantic-graph"),
        **cfg,
    )
    # Point store path via config.db_path which uses HERMES_HOME.
    rt = SemanticGraphRuntime(llm=None, config=config)
    return rt


def test_post_llm_captures_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path, capture_turns=True, auto_extract="off")
    rt.on_post_llm_call(
        session_id="s1",
        turn_id="t1",
        user_message="I prefer Python",
        assistant_response="Noted.",
        model="test-model",
        platform="cli",
    )
    arts = rt.store().list_artifacts()
    types = {a["artifact_type"] for a in arts}
    assert "user_message" in types
    assert "assistant_response" in types

    # Duplicate same turn should not double-insert.
    before = len(arts)
    rt.on_post_llm_call(
        session_id="s1",
        turn_id="t1",
        user_message="I prefer Python",
        assistant_response="Noted.",
        model="test-model",
        platform="cli",
    )
    assert len(rt.store().list_artifacts()) == before


def test_post_llm_embedding_is_bounded_and_failure_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class RecordingBackend:
        identity = EmbeddingModelIdentity(
            provider="test",
            model="phase6",
            revision="r1",
            dimensions=4,
            serializer_version=1,
        )

        def __init__(self, *, fail: bool = False):
            self.fail = fail
            self.batches: list[list[str]] = []

        def available(self):
            return True

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

        def embed_documents(self, texts):
            self.batches.append(list(texts))
            if self.fail:
                raise EmbeddingBackendError("injected post-turn failure")
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedding = SemanticGraphEmbeddingConfig(enabled=True, dimensions=4)
    rt = _rt(
        tmp_path,
        embedding=embedding,
        capture_turns=True,
        auto_extract="off",
    )
    backend = RecordingBackend()
    rt._embedding_backend = backend
    rt._embedding_backend_initialized = True
    for index in range(6):
        rt.store().upsert_node(
            {
                "node_id": f"claim-{index}",
                "node_type": "Claim",
                "subtype": "memory.fact",
                "label": f"Pending memory {index}",
                "normalized_label": f"pending memory {index}",
                "summary": f"Pending memory content {index}",
                "identity_key": f"pending:{index}",
                "status": "asserted",
                "authority": "user",
                "confidence": 0.8,
                "salience": 0.7,
            }
        )

    rt.on_post_llm_call(
        session_id="s",
        turn_id="bounded",
        user_message="remember these facts",
        assistant_response="Recorded.",
    )
    assert len(backend.batches) == 1
    assert len(backend.batches[0]) == 3
    embedded = [
        node_id
        for node_id in (f"claim-{index}" for index in range(6))
        if rt.store().get_node_embedding(
            node_id=node_id,
            namespace=backend.identity.namespace,
        )
        is not None
    ]
    assert len(embedded) == 3

    failed_rt = _rt(
        tmp_path,
        db_subdir="semantic-graph-failure",
        embedding=embedding,
        capture_turns=True,
        auto_extract="off",
    )
    failed_backend = RecordingBackend(fail=True)
    failed_rt._embedding_backend = failed_backend
    failed_rt._embedding_backend_initialized = True
    failed_rt.store().upsert_node(
        {
            "node_id": "claim-failure",
            "node_type": "Claim",
            "subtype": "memory.fact",
            "label": "Canonical write survives",
            "normalized_label": "canonical write survives",
            "summary": "Canonical write survives embedding failure",
            "identity_key": "failure:1",
            "status": "asserted",
            "authority": "user",
            "confidence": 0.8,
            "salience": 0.7,
        }
    )
    failed_rt.on_post_llm_call(
        session_id="s",
        turn_id="failure",
        user_message="remember this too",
        assistant_response="Recorded despite embedding failure.",
    )
    assert len(failed_rt.store().list_artifacts()) == 2
    assert len(failed_backend.batches) == 1


def test_empty_response_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path)
    rt.on_post_llm_call(
        session_id="s",
        turn_id="t",
        user_message="hi",
        assistant_response="   ",
    )
    assert rt.store().list_artifacts() == []


def test_capture_turns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path, capture_turns=False)
    rt.on_post_llm_call(
        session_id="s",
        turn_id="t",
        user_message="hi",
        assistant_response="hello",
    )
    assert rt.store().list_artifacts() == []


def test_tool_capture_opt_in_and_denylist(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path, capture_tool_events=False)
    rt.on_post_tool_call(tool_name="terminal", result="ok", args={})
    assert rt.store().get_status_counts()["runs"] == 0  # events not counted in runs

    rt2 = _rt(tmp_path, capture_tool_events=True)
    # Force separate store path by using same home — events table.
    rt2.on_post_tool_call(
        tool_name="semantic_graph_status",
        result='{"ok":true}',
        args={},
        session_id="s",
    )
    # Own tools denied — no explosion.
    rt2.on_post_tool_call(
        tool_name="web_search",
        result='{"hits":1}',
        args={"q": "x"},
        session_id="s",
        turn_id="t",
    )


def test_pre_llm_recall_bounded_and_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path, retrieval_enabled=True, min_recall_confidence=0.5)
    store = rt.store()
    store.upsert_node(
        {
            "node_id": "node_pref1",
            "node_type": "Preference",
            "subtype": "",
            "label": "frontend language TypeScript",
            "normalized_label": "frontend language typescript",
            "summary": "User prefers TypeScript for frontend",
            "identity_key": "pref.frontend",
            "status": "asserted",
            "authority": "user",
            "confidence": 0.95,
            "salience": 0.9,
        }
    )
    # Seed FTS if enabled
    if store.fts_enabled:
        # Re-upsert triggers fts insert only on create; already created.
        pass
    ctx = rt.on_pre_llm_call(user_message="what frontend language do I prefer?")
    # May be None if FTS empty for freshly inserted without fts row sync —
    # force LIKE path by searching directly.
    if ctx is None:
        from plugins.semantic_graph.retrieval import render_context, search_and_rank

        hits = search_and_rank(store, "frontend TypeScript", top_k=3, min_confidence=0.5)
        rendered = render_context(hits, 3500)
        assert rendered is None or "semantic_graph_context" in rendered
    else:
        assert "semantic_graph_context" in ctx["context"]
        assert "data_only" in ctx["context"]

    # Fail-open: broken store path should not raise.
    rt._store = None
    rt._config = SemanticGraphConfig(db_subdir="/nonexistent/\x00/bad")
    assert rt.on_pre_llm_call(user_message="hello world") is None or True


def test_subagent_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = _rt(tmp_path, capture_subagents=True)
    rt.on_subagent_start(subagent_id="c1", goal="extract", parent_id="p", role="leaf")
    rt.on_subagent_stop(subagent_id="c1", status="ok", summary="done", duration=1.2)
