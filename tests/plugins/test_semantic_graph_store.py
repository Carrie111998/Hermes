"""Store / migration tests for semantic-graph."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from plugins.semantic_graph.store import SCHEMA_VERSION, SemanticGraphStore


def _store(tmp_path: Path) -> SemanticGraphStore:
    db = tmp_path / "semantic-graph" / "semantic_graph.db"
    s = SemanticGraphStore(db)
    s.ensure_ready()
    return s


def test_first_use_creates_db_and_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = _store(tmp_path)
    counts = s.get_status_counts()
    assert counts["schema_version"] == SCHEMA_VERSION
    assert (tmp_path / "semantic-graph" / "semantic_graph.db").exists()
    assert "fts_enabled" in counts


def test_migration_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = _store(tmp_path)
    s.ensure_ready()
    s2 = SemanticGraphStore(tmp_path / "semantic-graph" / "semantic_graph.db")
    s2.ensure_ready()
    assert s2.get_status_counts()["schema_version"] == SCHEMA_VERSION


def test_duplicate_artifact_and_fragment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = _store(tmp_path)
    run = s.create_run(objective="t")
    a1 = s.upsert_artifact(
        {
            "artifact_type": "user_message",
            "content": "hello",
            "content_hash": "h1",
            "authority": "user",
            "session_id": "s",
            "turn_id": "t1",
            "run_id": run["run_id"],
        }
    )
    a2 = s.upsert_artifact(
        {
            "artifact_type": "user_message",
            "content": "hello",
            "content_hash": "h1",
            "authority": "user",
            "session_id": "s",
            "turn_id": "t1",
            "run_id": run["run_id"],
        }
    )
    assert a1["duplicate"] is False
    assert a2["duplicate"] is True
    assert a1["artifact_id"] == a2["artifact_id"]

    f1 = s.insert_fragment(
        {
            "run_id": run["run_id"],
            "producer_role": "x",
            "producer_type": "subagent",
            "payload_json": "{}",
            "payload_hash": "ph1",
        }
    )
    f2 = s.insert_fragment(
        {
            "run_id": run["run_id"],
            "producer_role": "x",
            "producer_type": "subagent",
            "payload_json": "{}",
            "payload_hash": "ph1",
        }
    )
    assert f1["duplicate"] is False
    assert f2["duplicate"] is True


def test_foreign_keys_and_concurrent_fragments(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    s = _store(tmp_path)
    run = s.create_run(objective="concurrent")
    errors: list[str] = []

    def worker(i: int) -> None:
        try:
            s.insert_fragment(
                {
                    "run_id": run["run_id"],
                    "producer_role": f"r{i}",
                    "producer_type": "subagent",
                    "payload_json": f'{{"i":{i}}}',
                    "payload_hash": f"hash-{i}",
                }
            )
        except Exception as exc:  # pragma: no cover
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(s.list_fragments_for_run(run["run_id"])) == 8

    # Invalid run_id should fail FK when inserting fragment.
    with pytest.raises(Exception):
        s.insert_fragment(
            {
                "run_id": "missing-run",
                "producer_role": "x",
                "producer_type": "subagent",
                "payload_json": "{}",
                "payload_hash": "x",
            }
        )
