from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from plugins.semantic_graph.cognitive import observe_cognitive_rerank
from plugins.semantic_graph.config import (
    SemanticGraphCognitiveMemoryConfig,
    SemanticGraphConfig,
)
from plugins.semantic_graph.runtime import SemanticGraphRuntime


def _state(
    memory_id: int,
    *,
    belief_version: int = 1,
    projected_retention: float | None = 0.8,
    confidence: float = 0.9,
    access_state: str = "accessible",
    belief_status: str = "current",
) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "belief_version": belief_version,
        "projected_retention": projected_retention,
        "confidence": confidence,
        "access_state": access_state,
        "belief_status": belief_status,
    }


def _link(memory_id: int, belief_version: int = 1) -> dict[str, object]:
    return {
        "memory_id": memory_id,
        "belief_version": belief_version,
        "relation": "represents",
    }


def test_shadow_uses_fixed_formula_and_deterministic_representative() -> None:
    candidates = [{"node_id": "node-a"}, {"node_id": "node-b"}]
    links = {
        "node-a": [_link(1, 5), _link(2, 2), _link(3, 3)],
        "node-b": [],
    }
    states = {
        1: _state(
            1,
            belief_version=5,
            projected_retention=0.99,
            confidence=1.0,
            belief_status="context_dependent",
        ),
        2: _state(2, belief_version=2, projected_retention=0.5),
        3: _state(3, belief_version=3, projected_retention=0.5),
    }

    observed = observe_cognitive_rerank(
        candidates,
        links_by_node=links,
        states_by_memory=states,
        query_mode="normal",
    )

    assert [row["node_id"] for row in observed] == ["node-a", "node-b"]
    first = observed[0]["cognitive_shadow"]
    assert first["base_rank"] == 1
    assert first["memory_link_count"] == 3
    assert first["representative_memory_id"] == 3
    assert first["projected_retention"] == 0.5
    assert first["cognitive_score"] == pytest.approx(
        (1.0 / 61.0) * (0.75 + 0.25 * 0.5) * (0.80 + 0.20 * 0.9)
    )
    assert first["cognitive_rank"] == 2
    assert first["rank_changed"] is True
    assert first["would_filter"] is False
    assert first["reason"] == "scored"
    assert observed[1]["cognitive_shadow"]["cognitive_rank"] == 1


def test_shadow_stale_or_missing_projection_falls_back_to_base_rank() -> None:
    observed = observe_cognitive_rerank(
        [{"node_id": "node-a"}],
        links_by_node={"node-a": [_link(7, 2)]},
        states_by_memory={7: _state(7, belief_version=2, projected_retention=None)},
        query_mode="normal",
    )

    shadow = observed[0]["cognitive_shadow"]
    assert shadow["memory_link_count"] == 1
    assert shadow["representative_memory_id"] is None
    assert shadow["projected_retention"] is None
    assert shadow["cognitive_score"] == pytest.approx(1.0 / 61.0)
    assert shadow["cognitive_rank"] == 1
    assert shadow["rank_changed"] is False
    assert shadow["reason"] == "stale_or_missing_state"


def test_shadow_marks_latent_and_noncurrent_without_bypassing_query_mode() -> None:
    candidates = [{"node_id": "latent"}, {"node_id": "historical"}]
    links = {
        "latent": [_link(1), _link(2)],
        "historical": [_link(3)],
    }
    states = {
        1: _state(1, access_state="latent"),
        2: _state(2, access_state="latent", projected_retention=0.9),
        3: _state(3, belief_status="retracted"),
    }

    normal = observe_cognitive_rerank(
        candidates,
        links_by_node=links,
        states_by_memory=states,
        query_mode="normal",
    )
    history = observe_cognitive_rerank(
        candidates,
        links_by_node=links,
        states_by_memory=states,
        query_mode="history",
    )
    rescue = observe_cognitive_rerank(
        candidates,
        links_by_node=links,
        states_by_memory=states,
        query_mode="rescue",
    )

    assert normal[0]["cognitive_shadow"]["would_filter"] is True
    assert normal[0]["cognitive_shadow"]["reason"] == "all_linked_latent"
    assert history[0]["cognitive_shadow"]["would_filter"] is True
    assert rescue[0]["cognitive_shadow"]["would_filter"] is False
    assert normal[1]["cognitive_shadow"]["would_filter"] is True
    assert normal[1]["cognitive_shadow"]["reason"] == "noncurrent_belief"
    assert history[1]["cognitive_shadow"]["would_filter"] is False


def _node(node_id: str, label: str, *, status: str = "asserted") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Claim",
        "subtype": "memory.fact",
        "label": label,
        "normalized_label": label.casefold(),
        "summary": label,
        "identity_key": node_id,
        "status": status,
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": {},
    }


def _database_dump(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_runtime_shadow_keeps_production_order_context_and_databases_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    base_config = SemanticGraphConfig(
        min_recall_confidence=0.0,
        cognitive_memory=SemanticGraphCognitiveMemoryConfig(),
    )
    baseline_runtime = SemanticGraphRuntime(config=base_config)
    store = baseline_runtime.store()
    store.upsert_node(_node("node-a", "Python alpha"))
    store.upsert_node(_node("node-b", "Python beta"))
    store.upsert_node(_node("node-rejected", "Python rejected", status="rejected"))
    store.upsert_memory_node_link(
        {
            "memory_id": 1,
            "node_id": "node-a",
            "belief_id": "belief-1",
            "belief_version": 1,
            "relation": "represents",
        }
    )
    store.upsert_memory_state_cache(
        {
            "memory_id": 1,
            "belief_id": "belief-1",
            "belief_version": 1,
            "access_state": "latent",
            "belief_status": "current",
            "memory_state": "active",
            "retention_at_sync": 0.8,
            "stability_days": 1.0,
            "salience": 0.8,
            "valence": 0.0,
            "confidence": 0.9,
            "protected": False,
            "source_updated_at": 100.0,
            "synced_at": 101.0,
        }
    )

    baseline = json.loads(baseline_runtime.handle_search({"query": "Python"}))
    baseline_context = baseline_runtime.on_pre_llm_call(user_message="Python")
    graph_path = base_config.db_path()
    before = _database_dump(graph_path)
    ebbinghaus_path = tmp_path / "ebbinghaus_memory.db"
    assert not ebbinghaus_path.exists()

    shadow_runtime = SemanticGraphRuntime(
        config=SemanticGraphConfig(
            min_recall_confidence=0.0,
            cognitive_memory=SemanticGraphCognitiveMemoryConfig(
                rerank_enabled=True,
                mode="shadow",
            ),
        )
    )
    shadow = json.loads(shadow_runtime.handle_search({"query": "Python"}))
    shadow_context = shadow_runtime.on_pre_llm_call(user_message="Python")

    assert [row["node_id"] for row in shadow["results"]] == [
        row["node_id"] for row in baseline["results"]
    ]
    assert all("cognitive_shadow" in row for row in shadow["results"])
    assert all(row["node_id"] != "node-rejected" for row in shadow["results"])
    assert shadow_context == baseline_context
    assert _database_dump(graph_path) == before
    assert not ebbinghaus_path.exists()


def test_cognitive_config_accepts_only_off_shadow_and_active_modes() -> None:
    assert SemanticGraphCognitiveMemoryConfig(mode="off").mode == "off"
    assert SemanticGraphCognitiveMemoryConfig(mode="shadow").mode == "shadow"
    assert SemanticGraphCognitiveMemoryConfig(mode="active").mode == "active"
    with pytest.raises(ValueError):
        SemanticGraphCognitiveMemoryConfig(mode="production")
