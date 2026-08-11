from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugins.semantic_graph import store as store_module
from plugins.semantic_graph.embedding import EmbeddingModelIdentity
from plugins.semantic_graph.embedding.vectors import pack_float32_le
from plugins.semantic_graph.store import (
    DB_SCHEMA_VERSION,
    DDL_CORE,
    MIGRATION_V2_STATEMENTS,
    SemanticGraphStore,
)


def _node(node_id: str = "node-existing") -> dict[str, object]:
    return {
        "node_id": node_id,
        "node_type": "Claim",
        "subtype": "memory.fact",
        "label": "Existing node",
        "normalized_label": "existing node",
        "summary": "Existing data must survive migration",
        "identity_key": "existing",
        "status": "asserted",
        "authority": "user",
        "confidence": 0.9,
        "salience": 0.8,
        "metadata": {},
    }


def _create_v2_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(DDL_CORE)
        for statement in MIGRATION_V2_STATEMENTS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 2")
        columns = (
            "node_id, node_type, subtype, label, normalized_label, summary, "
            "identity_key, status, authority, confidence, salience, "
            "metadata_json, created_at, updated_at"
        )
        conn.execute(
            f"INSERT INTO nodes({columns}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "node-existing",
                "Claim",
                "memory.fact",
                "Existing node",
                "existing node",
                "Existing data must survive migration",
                "existing",
                "asserted",
                "user",
                0.9,
                0.8,
                "{}",
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO node_embeddings(
                node_id, namespace, provider, model, revision,
                serializer_version, dimensions, dtype, vector_blob,
                source_text_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "node-existing",
                EmbeddingModelIdentity("test", "existing", "r1", 3, 1).namespace,
                "test",
                "existing",
                "r1",
                1,
                3,
                "float32-le",
                pack_float32_le([1.0, 0.0, 0.0]),
                "a" * 64,
                "2026-08-12T00:00:00+00:00",
                "2026-08-12T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_v2_migrates_additively_and_preserves_graph_and_embedding(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v2_database(db)

    store = SemanticGraphStore(db)
    store.ensure_ready()

    assert DB_SCHEMA_VERSION == 3
    assert store.get_node("node-existing") is not None
    identity = EmbeddingModelIdentity("test", "existing", "r1", 3, 1)
    assert store.get_node_embedding(
        node_id="node-existing",
        namespace=identity.namespace,
    ) is not None
    assert store.get_status_counts() == {
        **store.get_status_counts(),
        "schema_version": 3,
        "memory_node_links": 0,
        "memory_state_cache": 0,
    }


def test_v3_link_and_state_upserts_are_idempotent_and_fk_cascades(tmp_path: Path) -> None:
    store = SemanticGraphStore(tmp_path / "semantic_graph.db")
    store.ensure_ready()
    store.upsert_node(_node())

    link = {
        "memory_id": 7,
        "node_id": "node-existing",
        "belief_id": "belief-7",
        "belief_version": 2,
        "relation": "represents",
    }
    state = {
        "memory_id": 7,
        "belief_id": "belief-7",
        "belief_version": 2,
        "access_state": "accessible",
        "belief_status": "current",
        "memory_state": "active",
        "retention_at_sync": 0.75,
        "stability_days": 3.0,
        "salience": 0.8,
        "valence": 0.1,
        "confidence": 0.9,
        "protected": True,
        "source_updated_at": 100.0,
        "synced_at": 101.0,
    }

    store.upsert_memory_node_link(link)
    store.upsert_memory_node_link(link)
    store.upsert_memory_state_cache(state)
    store.upsert_memory_state_cache(state)

    assert store.get_memory_node_links(memory_id=7) == [
        {**store.get_memory_node_links(memory_id=7)[0], "memory_id": 7}
    ]
    cached = store.get_memory_state_cache(7)
    assert cached is not None
    assert cached["belief_version"] == 2
    assert cached["protected"] == 1
    assert store.get_status_counts()["memory_node_links"] == 1
    assert store.get_status_counts()["memory_state_cache"] == 1

    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_memory_node_link({**link, "memory_id": 8, "node_id": "missing"})

    with store._connect() as conn:  # noqa: SLF001 - migration/FK contract test
        conn.execute("DELETE FROM nodes WHERE node_id = ?", ("node-existing",))
    assert store.get_memory_node_links(memory_id=7) == []
    assert store.get_memory_state_cache(7) is not None


def test_v3_migration_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v2_database(db)

    SemanticGraphStore(db).ensure_ready()
    SemanticGraphStore(db).ensure_ready()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('memory_node_links', 'memory_state_cache')"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_v3_migration_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v2_database(db)
    monkeypatch.setattr(
        store_module,
        "MIGRATION_V3_STATEMENTS",
        (
            "CREATE TABLE memory_node_links(memory_id INTEGER)",
            "THIS IS NOT VALID SQL",
        ),
    )

    with pytest.raises(sqlite3.Error):
        SemanticGraphStore(db).ensure_ready()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='memory_node_links'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='memory_state_cache'"
        ).fetchone() is None
    finally:
        conn.close()
