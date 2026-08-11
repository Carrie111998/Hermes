"""SQLite embedding migration and invalidation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from plugins.semantic_graph.store import (
    DB_SCHEMA_VERSION,
    DDL_CORE,
    GRAPH_SCHEMA_VERSION,
    SemanticGraphStore,
)


_NODE = {
    "node_id": "node-existing",
    "node_type": "Preference",
    "subtype": "development.frontend.language",
    "label": "Frontend language",
    "normalized_label": "frontend language",
    "summary": "User prefers TypeScript",
    "identity_key": "preference.frontend.language",
    "status": "asserted",
    "authority": "user",
    "confidence": 0.95,
    "salience": 0.90,
    "metadata_json": "{}",
    "created_at": "2026-08-09T00:00:00+00:00",
    "updated_at": "2026-08-09T00:00:00+00:00",
}


def _create_v1_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(DDL_CORE)
        conn.execute("PRAGMA user_version = 1")
        columns = ", ".join(_NODE)
        placeholders = ", ".join("?" for _ in _NODE)
        conn.execute(
            f"INSERT INTO nodes({columns}) VALUES ({placeholders})",
            tuple(_NODE.values()),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_embedding(path: Path, node_id: str = "node-existing") -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            INSERT INTO node_embeddings(
                node_id, namespace, provider, model, revision,
                serializer_version, dimensions, dtype, vector_blob,
                source_text_hash, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                "test:fake:v1:d3:s1",
                "test",
                "fake",
                "v1",
                1,
                3,
                "float32-le",
                b"\x00" * 12,
                "a" * 64,
                "2026-08-09T00:00:00+00:00",
                "2026-08-09T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _embedding_count(path: Path, node_id: str = "node-existing") -> int:
    conn = sqlite3.connect(path)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM node_embeddings WHERE node_id = ?",
                (node_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_fresh_database_is_created_at_current_schema(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    SemanticGraphStore(db).ensure_ready()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='node_embeddings'"
        ).fetchone() == ("node_embeddings",)
    finally:
        conn.close()


def test_v1_database_migrates_without_losing_nodes(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    SemanticGraphStore(db).ensure_ready()

    assert SemanticGraphStore(db).get_node("node-existing") is not None
    assert _embedding_count(db) == 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM graph_runs").fetchone()[0] == 0
        assert conn.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    finally:
        conn.close()


def test_graph_schema_version_is_separate_from_db_version(tmp_path: Path) -> None:
    store = SemanticGraphStore(tmp_path / "semantic_graph.db")
    store.ensure_ready()
    run = store.create_run(objective="test")
    status = store.get_status_counts()

    assert status["schema_version"] == DB_SCHEMA_VERSION
    assert status["graph_schema_version"] == GRAPH_SCHEMA_VERSION
    assert store.get_run(run["run_id"])["schema_version"] == GRAPH_SCHEMA_VERSION


def test_database_migrations_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    SemanticGraphStore(db).ensure_ready()
    SemanticGraphStore(db).ensure_ready()
    assert _embedding_count(db) == 0


def test_node_delete_cascades_to_embeddings(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    store = SemanticGraphStore(db)
    store.ensure_ready()
    _insert_embedding(db)

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("DELETE FROM nodes WHERE node_id = ?", ("node-existing",))
        conn.commit()
    finally:
        conn.close()

    assert _embedding_count(db) == 0


def test_embedding_blob_length_must_match_dimensions(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    SemanticGraphStore(db).ensure_ready()
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO node_embeddings(
                    node_id, namespace, provider, model, revision,
                    serializer_version, dimensions, dtype, vector_blob,
                    source_text_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "node-existing", "bad", "test", "fake", "v1", 1, 3,
                    "float32-le", b"\x00" * 8, "b" * 64,
                    "now", "now",
                ),
            )
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("column", "value"),
    [("dimensions", 0), ("serializer_version", 0)],
)
def test_embedding_positive_integer_constraints(
    tmp_path: Path,
    column: str,
    value: int,
) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    SemanticGraphStore(db).ensure_ready()
    values = {
        "node_id": "node-existing",
        "namespace": f"bad-{column}",
        "provider": "test",
        "model": "fake",
        "revision": "v1",
        "serializer_version": 1,
        "dimensions": 3,
        "dtype": "float32-le",
        "vector_blob": b"\x00" * 12,
        "source_text_hash": "c" * 64,
        "created_at": "now",
        "updated_at": "now",
    }
    values[column] = value
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO node_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(values.values()),
            )
    finally:
        conn.close()


def test_embedding_dtype_is_fixed(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    SemanticGraphStore(db).ensure_ready()
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO node_embeddings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "node-existing", "bad-dtype", "test", "fake", "v1", 1, 3,
                    "float16", b"\x00" * 12, "d" * 64, "now", "now",
                ),
            )
    finally:
        conn.close()


def test_invalid_existing_embedding_shape_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """
            CREATE TABLE node_embeddings (
                node_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                revision TEXT NOT NULL DEFAULT '',
                serializer_version INTEGER NOT NULL,
                dimensions INTEGER NOT NULL,
                dtype TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                source_text_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="invalid node_embeddings schema"):
        SemanticGraphStore(db).ensure_ready()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_v2_migration_rolls_back_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from plugins.semantic_graph import store as store_module

    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    monkeypatch.setattr(
        store_module,
        "MIGRATION_V2_STATEMENTS",
        (store_module.MIGRATION_V2_STATEMENTS[0], "THIS IS NOT VALID SQL"),
    )

    with pytest.raises(sqlite3.Error):
        SemanticGraphStore(db).ensure_ready()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='node_embeddings'"
        ).fetchone() is None
    finally:
        conn.close()


def test_newer_database_version_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        SemanticGraphStore(db).ensure_ready()


def test_semantic_node_update_invalidates_all_embeddings(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    store = SemanticGraphStore(db)
    store.ensure_ready()
    _insert_embedding(db)

    existing = store.get_node("node-existing")
    assert existing is not None
    store.upsert_node({**existing, "summary": "User strongly prefers TypeScript for frontend development"})

    assert _embedding_count(db) == 0


def test_nonsemantic_node_update_keeps_embeddings(tmp_path: Path) -> None:
    db = tmp_path / "semantic_graph.db"
    _create_v1_database(db)
    store = SemanticGraphStore(db)
    store.ensure_ready()
    _insert_embedding(db)

    existing = store.get_node("node-existing")
    assert existing is not None
    store.upsert_node({**existing, "status": "accepted", "confidence": 0.99, "salience": 1.0})

    assert _embedding_count(db) == 1
