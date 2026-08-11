from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pytest

from plugins.memory.ebbinghaus import EbbinghausMemoryProvider
from plugins.memory.ebbinghaus.semantic_graph_bridge import (
    EbbinghausSemanticGraphBridge,
    project_retention,
)
from plugins.memory.ebbinghaus.store import EbbinghausMemoryStore
from plugins.semantic_graph import config as graph_config_module
from plugins.semantic_graph.config import (
    SemanticGraphCognitiveMemoryConfig,
    SemanticGraphConfig,
)
from plugins.semantic_graph.cli import register_cli
from plugins.semantic_graph.runtime import SemanticGraphRuntime
from plugins.semantic_graph.store import SemanticGraphStore


def _bridge(tmp_path: Path):
    memory = EbbinghausMemoryStore(
        tmp_path / "ebbinghaus_memory.db",
        time_fn=lambda: 1_700_000_000.0,
    )
    graph = SemanticGraphStore(tmp_path / "semantic-graph" / "semantic_graph.db")
    graph.ensure_ready()
    return memory, graph, EbbinghausSemanticGraphBridge(graph, memory_store=memory)


def test_cognitive_config_defaults_are_disabled_and_nested_values_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = SemanticGraphConfig().cognitive_memory
    assert defaults == SemanticGraphCognitiveMemoryConfig()
    assert defaults.bridge_enabled is False
    assert defaults.rerank_enabled is False
    assert defaults.mode == "shadow"
    assert defaults.abstention_enabled is False

    monkeypatch.setattr(
        graph_config_module,
        "_raw_plugin_config",
        lambda: {
            "cognitive_memory": {
                "bridge_enabled": True,
                "rerank_enabled": True,
                "mode": "shadow",
                "abstention_enabled": True,
            }
        },
    )
    parsed = graph_config_module.load_config().cognitive_memory
    assert parsed.bridge_enabled is True
    assert parsed.rerank_enabled is True
    assert parsed.abstention_enabled is True


def test_bridge_mapping_link_cache_and_sync_are_idempotent(tmp_path: Path) -> None:
    memory, graph, bridge = _bridge(tmp_path)
    try:
        remembered = memory.remember(
            "User prefers concise Japanese responses",
            tags=["preference", "user", "pinned"],
            salience=0.9,
            source="tool",
        )

        first = bridge.after_remember(remembered)
        second = bridge.after_remember(remembered)

        assert first["success"] is True
        assert second["success"] is True
        links = graph.get_memory_node_links(memory_id=remembered["memory_id"])
        assert len(links) == 1
        node = graph.get_node(links[0]["node_id"])
        assert node is not None
        assert node["node_type"] == "Preference"
        assert node["identity_key"] == (
            f"ebbinghaus:{links[0]['belief_id']}:v{links[0]['belief_version']}"
        )
        assert node["status"] == "asserted"
        cache = graph.get_memory_state_cache(remembered["memory_id"])
        assert cache is not None
        assert cache["belief_id"] == remembered["belief_id"]
        assert cache["protected"] == 1
        assert graph.get_status_counts()["memory_node_links"] == 1
        assert graph.get_status_counts()["memory_state_cache"] == 1
    finally:
        memory.close()


def test_revision_retraction_and_dream_provenance_mapping(tmp_path: Path) -> None:
    memory, graph, bridge = _bridge(tmp_path)
    try:
        old = memory.remember("The deployment window is Monday", tags=["decision"])
        bridge.after_remember(old)
        revised = memory.revise_memory(
            old["memory_id"],
            "The deployment window is Tuesday",
            reason="User corrected the schedule",
            confidence=0.95,
        )

        assert bridge.after_revision(revised)["success"] is True
        old_link = graph.get_memory_node_links(memory_id=old["memory_id"])[0]
        new_link = graph.get_memory_node_links(memory_id=revised["new_memory_id"])[0]
        assert graph.get_node(old_link["node_id"])["status"] == "superseded"
        assert graph.get_node(new_link["node_id"])["status"] == "asserted"
        supersedes = [
            edge
            for edge in graph.list_edges(include_rejected=True)
            if edge["edge_type"] == "supersedes"
        ]
        assert len(supersedes) == 1
        assert supersedes[0]["source_node_id"] == new_link["node_id"]
        assert supersedes[0]["target_node_id"] == old_link["node_id"]

        retracted = memory.retract_memory(
            revised["new_memory_id"], reason="Schedule was cancelled"
        )
        assert bridge.after_retraction(retracted)["success"] is True
        assert graph.get_node(new_link["node_id"])["status"] == "rejected"
        assert graph.get_memory_state_cache(revised["new_memory_id"])[
            "belief_status"
        ] == "retracted"

        source_a = memory.remember("Source memory alpha")
        source_b = memory.remember("Source memory beta")
        semantic = memory.remember(
            "Validated combined semantic insight",
            tags=["dream-summary", "semantic"],
            memory_type="semantic",
            source="dream:test",
        )
        memory._conn.executemany(  # noqa: SLF001 - seed existing provenance contract
            "INSERT INTO memory_provenance(semantic_memory_id, source_memory_id, relation, created_at) "
            "VALUES (?, ?, 'dream-derived', ?)",
            [
                (semantic["memory_id"], source_a["memory_id"], 1_700_000_000.0),
                (semantic["memory_id"], source_b["memory_id"], 1_700_000_000.0),
            ],
        )
        memory._conn.commit()  # noqa: SLF001

        dream = {
            "mode": "dream_apply",
            "enabled": True,
            "applied": [
                {
                    "status": "applied",
                    "semantic_memory_id": semantic["memory_id"],
                }
            ],
        }
        assert bridge.after_dream_apply(dream)["success"] is True
        derived = [
            edge
            for edge in graph.list_edges(include_rejected=True)
            if edge["edge_type"] == "derived_from"
        ]
        assert len(derived) == 2
        assert len(graph.get_memory_node_links(memory_id=semantic["memory_id"])) == 1
    finally:
        memory.close()


def test_dream_preview_has_zero_graph_mutation_and_apply_metadata_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    memory, graph, bridge = _bridge(tmp_path)
    try:
        source_a = memory.remember(
            "Episode A supports one reusable concept",
            tags=["topic", "source"],
            salience=0.8,
        )
        source_b = memory.remember(
            "Episode B supports the same reusable concept",
            tags=["topic", "source"],
            salience=0.75,
        )
        assert bridge.after_remember(source_a)["success"] is True
        assert bridge.after_remember(source_b)["success"] is True
        memory._conn.execute(  # noqa: SLF001 - seed existing dream contract
            "UPDATE memories SET dream_candidate = 1 WHERE memory_id IN (?, ?)",
            (source_a["memory_id"], source_b["memory_id"]),
        )
        memory._conn.commit()  # noqa: SLF001

        before_preview = graph.get_status_counts()
        preview = memory.dream_preview()
        assert graph.get_status_counts() == before_preview

        cluster = preview["clusters"][0]
        payload = {
            "cluster_id": cluster["cluster_id"],
            "source_memory_ids": cluster["source_memory_ids"],
            "summary": "A validated reusable concept from two episodes.",
            "tags": ["dream-summary", "semantic", "concept"],
            "salience": 0.75,
            "valence": 0.0,
        }
        applied = memory.dream_apply([payload])
        outcome = bridge.after_dream_apply(applied)
        assert outcome["success"] is True

        item = applied["applied"][0]
        semantic_id = item["semantic_memory_id"]
        semantic_link = graph.get_memory_node_links(memory_id=semantic_id)[0]
        semantic_node = graph.get_node(semantic_link["node_id"])
        assert semantic_node is not None
        assert semantic_node["node_type"] == "Concept"
        metadata = json.loads(semantic_node["metadata_json"])
        required = {
            "source_memory_ids",
            "source_graph_node_ids",
            "provenance",
            "applied_at",
            "embedding_namespace",
            "validation_state",
            "idempotency_key",
        }
        assert required <= metadata.keys()
        assert metadata["source_memory_ids"] == sorted(cluster["source_memory_ids"])
        assert len(metadata["source_graph_node_ids"]) == 2
        assert metadata["embedding_namespace"].startswith("llama.cpp:")
        assert metadata["validation_state"] == "apply_validated"
        assert item["source_graph_node_ids"] == [
            value.replace(":", "")
            for value in metadata["source_graph_node_ids"]
        ]
        assert item["embedding_namespace"] == metadata["embedding_namespace"]

        derived = [
            edge
            for edge in graph.list_edges(include_rejected=True)
            if edge["edge_type"] == "derived_from"
            and edge["source_node_id"] == semantic_link["node_id"]
        ]
        assert len(derived) == 2
        for edge in derived:
            edge_metadata = json.loads(edge["metadata_json"])
            assert required <= edge_metadata.keys()
            assert graph.get_node(edge["target_node_id"])["node_type"] == "Event"
        assert {
            item["source_graph_node_id"].replace(":", "")
            for item in metadata["provenance"]
        } == set(item["source_graph_node_ids"])
        assert {
            item["relation"] for item in metadata["provenance"]
        } == {"dream-derived"}

        counts_after_first = graph.get_status_counts()
        repeated = memory.dream_apply([payload])
        repeated_outcome = bridge.after_dream_apply(repeated)
        assert repeated["applied"][0]["status"] == "idempotent"
        assert repeated_outcome["success"] is True
        assert graph.get_status_counts() == counts_after_first
        assert memory.get(source_a["memory_id"])["state"] == "archived"
        assert memory.get(source_b["memory_id"])["state"] == "archived"
    finally:
        memory.close()


def test_bridge_failure_never_rolls_back_canonical_write_and_repair_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory, graph, bridge = _bridge(tmp_path)
    provider = EbbinghausMemoryProvider(config={"db_path": str(memory.db_path)})
    provider._store = memory  # noqa: SLF001 - provider surface integration test
    provider._bridge = bridge  # noqa: SLF001
    original_upsert = graph.upsert_node

    def fail_node(_node):
        raise RuntimeError("injected graph failure containing private-memory-id")

    monkeypatch.setattr(graph, "upsert_node", fail_node)
    rendered = provider.handle_tool_call(
        "ebbinghaus_memory",
        {
            "action": "remember",
            "content": "Canonical memory survives graph failure",
            "tags": "user",
        },
    )
    result = json.loads(rendered)

    assert result["status"] == "remembered"
    assert memory.get(result["memory_id"])["content"] == (
        "Canonical memory survives graph failure"
    )
    pending = memory.list_events(event_type="semantic_graph_bridge_pending", limit=10)
    assert len(pending) == 1
    assert pending[0]["payload"]["operation"] == "remember"
    assert "private-memory-id" not in json.dumps(pending, ensure_ascii=False)

    before_graph = graph.get_status_counts()
    before_events = len(memory.list_events(limit=100))
    dry = bridge.repair(limit=10, dry_run=True)
    assert dry["would_repair"] == 1
    assert graph.get_status_counts() == before_graph
    assert len(memory.list_events(limit=100)) == before_events

    monkeypatch.setattr(graph, "upsert_node", original_upsert)
    applied = bridge.repair(limit=10, dry_run=False)
    repeated = bridge.repair(limit=10, dry_run=False)
    assert applied["repaired"] == 1
    assert repeated["repaired"] == 0
    assert graph.get_memory_node_links(memory_id=result["memory_id"])
    provider._store = None  # noqa: SLF001 - avoid double-close in shutdown
    memory.close()


def test_sync_dry_run_has_zero_mutation_and_apply_is_idempotent(tmp_path: Path) -> None:
    memory, graph, bridge = _bridge(tmp_path)
    try:
        memory.remember("A durable fact", tags=["semantic"])
        memory.remember("A durable procedure", tags=["procedure"])
        graph_before = graph.get_status_counts()
        event_count = len(memory.list_events(limit=100))

        dry = bridge.sync(limit=10, dry_run=True)

        assert dry["would_sync"] == 2
        assert graph.get_status_counts() == graph_before
        assert len(memory.list_events(limit=100)) == event_count

        first = bridge.sync(limit=10, dry_run=False)
        second = bridge.sync(limit=10, dry_run=False)
        assert first["synced"] == 2
        assert second["synced"] == 2
        assert graph.get_status_counts()["memory_node_links"] == 2
        assert graph.get_status_counts()["memory_state_cache"] == 2
    finally:
        memory.close()


def test_projection_rejects_missing_stale_nonfinite_and_version_mismatch() -> None:
    cache = {
        "belief_version": 2,
        "retention_at_sync": 1.0,
        "stability_days": 1.0,
        "source_updated_at": 90.0,
        "synced_at": 100.0,
    }
    assert project_retention(
        cache,
        now=100.0 + 86_400.0,
        expected_belief_version=2,
    ) == pytest.approx(math.exp(-1.0))
    assert project_retention(None, now=100.0) is None
    assert project_retention(
        {**cache, "source_updated_at": 101.0}, now=102.0
    ) is None
    assert project_retention(
        {**cache, "retention_at_sync": float("nan")}, now=102.0
    ) is None
    assert project_retention(
        cache, now=102.0, expected_belief_version=3
    ) is None


def test_temp_hermes_home_provider_to_graph_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        graph_config_module,
        "_raw_plugin_config",
        lambda: {"cognitive_memory": {"bridge_enabled": True}},
    )
    provider = EbbinghausMemoryProvider(
        config={"db_path": "$HERMES_HOME/ebbinghaus_memory.db"}
    )
    provider.initialize("temp-session", hermes_home=str(tmp_path))
    try:
        result = json.loads(
            provider.handle_tool_call(
                "ebbinghaus_memory",
                {
                    "action": "remember",
                    "content": "Temporary HOME end-to-end preference",
                    "tags": "preference,user",
                },
            )
        )
        graph = SemanticGraphStore(
            tmp_path / "semantic-graph" / "semantic_graph.db"
        )
        links = graph.get_memory_node_links(memory_id=result["memory_id"])
        assert len(links) == 1
        assert graph.get_node(links[0]["node_id"])["node_type"] == "Preference"
    finally:
        provider.shutdown()


def test_phase3_runtime_search_order_ignores_cognitive_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runtime = SemanticGraphRuntime(
        config=SemanticGraphConfig(
            min_recall_confidence=0.0,
            cognitive_memory=SemanticGraphCognitiveMemoryConfig(
                bridge_enabled=True,
                rerank_enabled=False,
            ),
        )
    )
    for index, label in enumerate(("Python frontend", "Python backend"), start=1):
        runtime.store().upsert_node(
            {
                "node_id": f"node-{index}",
                "node_type": "Claim",
                "subtype": "test",
                "label": label,
                "normalized_label": label.casefold(),
                "summary": label,
                "identity_key": f"test-{index}",
                "status": "asserted",
                "authority": "user",
                "confidence": 0.9,
                "salience": 0.8,
                "metadata": {},
            }
        )
    before = json.loads(runtime.handle_search({"query": "Python", "top_k": 8}))
    runtime.store().upsert_memory_node_link(
        {
            "memory_id": 1,
            "node_id": "node-2",
            "belief_id": "belief-1",
            "belief_version": 1,
            "relation": "represents",
        }
    )
    runtime.store().upsert_memory_state_cache(
        {
            "memory_id": 1,
            "belief_id": "belief-1",
            "belief_version": 1,
            "access_state": "latent",
            "belief_status": "current",
            "memory_state": "active",
            "retention_at_sync": 0.01,
            "stability_days": 0.1,
            "salience": 0.1,
            "valence": 0.0,
            "confidence": 0.1,
            "protected": False,
            "source_updated_at": 100.0,
            "synced_at": 101.0,
        }
    )
    after = json.loads(runtime.handle_search({"query": "Python", "top_k": 8}))

    assert [row["node_id"] for row in after["results"]] == [
        row["node_id"] for row in before["results"]
    ]


def test_cognitive_cli_exposes_only_status_sync_and_repair_modes() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Runtime:
        def handle_cognitive_status(self, args):
            calls.append(("status", args))
            return '{"success":true}'

        def handle_cognitive_sync(self, args):
            calls.append(("sync", args))
            return '{"success":true}'

        def handle_cognitive_repair(self, args):
            calls.append(("repair", args))
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

    assert handler(parser.parse_args(["cognitive-status"])) == 0
    assert handler(
        parser.parse_args(["cognitive-sync", "--limit", "7", "--dry-run"])
    ) == 0
    assert handler(
        parser.parse_args(["cognitive-sync", "--limit", "3", "--apply"])
    ) == 0
    assert handler(
        parser.parse_args(["cognitive-repair", "--limit", "5", "--dry-run"])
    ) == 0
    assert handler(
        parser.parse_args(["cognitive-repair", "--limit", "2", "--apply"])
    ) == 0
    assert calls == [
        ("status", {}),
        ("sync", {"limit": 7, "dry_run": True, "apply": False}),
        ("sync", {"limit": 3, "dry_run": False, "apply": True}),
        ("repair", {"limit": 5, "dry_run": True, "apply": False}),
        ("repair", {"limit": 2, "dry_run": False, "apply": True}),
    ]
    with pytest.raises(SystemExit):
        parser.parse_args(["cognitive-sync", "--limit", "1"])
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["cognitive-repair", "--limit", "1", "--dry-run", "--apply"]
        )
