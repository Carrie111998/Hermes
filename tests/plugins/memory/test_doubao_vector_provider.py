"""Tests for the Doubao Vector memory provider."""

import json
from pathlib import Path
from typing import cast, Any

from plugins.memory.doubao_vector import DoubaoVectorMemoryProvider


class FakeEmbeddingClient:
    def embed(self, text: str):
        # Deterministic tiny embedding: queries about Hermes/vector align with stored text.
        lowered = text.lower()
        return [
            1.0 if "hermes" in lowered or "向量" in text else 0.0,
            1.0 if "辉哥" in text or "直接" in text else 0.0,
            0.5,
        ]


def test_doubao_vector_store_search_and_prefetch(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_EMBEDDING_API_KEY", "test-key")
    provider = DoubaoVectorMemoryProvider()
    provider.initialize("sess-1", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    provider._client = cast(Any, FakeEmbeddingClient())

    stored = json.loads(provider.handle_tool_call(
        "doubao_vector_store",
        {"content": "Hermes 向量记忆已经接入，辉哥要求直接看真实结果。", "target": "system"},
    ))
    assert stored["saved"] is True
    assert stored["dim"] == 3

    searched = json.loads(provider.handle_tool_call(
        "doubao_vector_search",
        {"query": "Hermes 向量记忆接入情况", "limit": 3},
    ))
    assert searched["count"] == 1
    assert "Hermes 向量记忆" in searched["results"][0]["content"]

    prefetched = provider.prefetch("Hermes 向量记忆接入情况")
    assert "<doubao-vector-memory>" in prefetched
    assert "Hermes 向量记忆" in prefetched

    index_path = Path(tmp_path) / "doubao_vector_memory" / "index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(index["items"]) == 1
    assert len(index["items"][0]["embedding"]) == 3


def test_doubao_vector_sync_turn_captures_user_only_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("DOUBAO_EMBEDDING_API_KEY", "test-key")
    provider = DoubaoVectorMemoryProvider()
    provider.initialize("sess-2", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    provider._client = cast(Any, FakeEmbeddingClient())

    provider.sync_turn("辉哥喜欢直接执行", "assistant response should not be captured by default", session_id="sess-2")

    stats = json.loads(provider.handle_tool_call("doubao_vector_stats", {}))
    assert stats["count"] == 1
    results = json.loads(provider.handle_tool_call("doubao_vector_search", {"query": "辉哥沟通偏好", "limit": 5}))
    assert results["count"] == 1
    assert results["results"][0]["target"] == "turn:user"
