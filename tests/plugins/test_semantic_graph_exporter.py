"""Export-focused tests (also covered in inference suite)."""

from __future__ import annotations

from pathlib import Path

import pytest

from plugins.semantic_graph.config import SemanticGraphConfig
from plugins.semantic_graph.exporter import ExportPathError, export_graph
from plugins.semantic_graph.runtime import SemanticGraphRuntime


def test_export_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = SemanticGraphRuntime(config=SemanticGraphConfig(db_subdir="semantic-graph"))
    store = rt.store()
    store.upsert_node(
        {
            "node_id": "node_x",
            "node_type": "Claim",
            "subtype": "",
            "label": "X",
            "normalized_label": "x",
            "summary": "claim",
            "status": "accepted",
            "authority": "user",
            "confidence": 0.9,
            "salience": 0.5,
        }
    )
    root = tmp_path / "semantic-graph" / "exports"
    for fmt in ("json", "jsonl", "markdown"):
        out = export_graph(store, format=fmt, export_root=root, include_rejected=False)
        assert Path(out["path"]).exists()
        assert out["nodes"] >= 1
