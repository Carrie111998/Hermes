"""Inference + finalize/eval/export/cli/skill tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.semantic_graph.config import SemanticGraphConfig
from plugins.semantic_graph.exporter import ExportPathError, export_graph
from plugins.semantic_graph.inference import SemanticGraphInference, SemanticGraphInferenceError
from plugins.semantic_graph.runtime import SemanticGraphRuntime
from plugins.semantic_graph.schemas import GRAPH_FRAGMENT_SCHEMA, OUTPUT_EVALUATION_SCHEMA


class FakeLLM:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(parsed=self.parsed, text=json.dumps(self.parsed))


def test_extract_uses_schema_no_override_no_retry():
    frag = {"summary": "s", "nodes": [], "edges": []}
    llm = FakeLLM(frag)
    inf = SemanticGraphInference(llm=llm)
    out = inf.extract_fragment("payload")
    assert out == frag
    call = llm.calls[0]
    assert call["json_schema"] == GRAPH_FRAGMENT_SCHEMA
    assert call["temperature"] == 0.0
    assert call["purpose"] == "semantic_graph.extract"
    assert "provider" not in call or call.get("provider") is None
    assert "model" not in call or call.get("model") is None
    assert len(llm.calls) == 1


def test_extract_none_errors():
    llm = FakeLLM(None)
    inf = SemanticGraphInference(llm=llm)
    with pytest.raises(SemanticGraphInferenceError):
        inf.extract_fragment("x")


def test_evaluate_and_no_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parsed = {
        "verdict": "fail",
        "overall_score": 0.2,
        "criteria": [{"name": "groundedness", "score": 0.1, "notes": "overbroad"}],
        "claims": [
            {
                "claim_text": "Python is always faster",
                "support": "unsupported",
                "evidence_ids": [],
                "notes": "overbroad",
            }
        ],
        "suggested_revision": "Qualify the claim.",
        "confidence": 0.8,
    }
    rt = SemanticGraphRuntime(
        llm=FakeLLM(parsed),
        config=SemanticGraphConfig(db_subdir="semantic-graph"),
    )
    art = rt.store().upsert_artifact(
        {
            "artifact_type": "assistant_output",
            "content": "Python is always faster than every JavaScript runtime.",
            "content_hash": "h",
            "authority": "assistant",
            "session_id": "s",
            "turn_id": "t",
        }
    )
    result = json.loads(
        rt.handle_evaluate_output({"artifact_id": art["artifact_id"], "store_result": True})
    )
    assert result["success"] is True
    assert result["artifact_rewritten"] is False
    assert result["evaluation"]["verdict"] == "fail"
    # Artifact content unchanged
    again = rt.store().get_artifact(art["artifact_id"])
    assert "always faster" in again["content"]


def test_finalize_promotion_user_asserted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = SemanticGraphRuntime(config=SemanticGraphConfig(db_subdir="semantic-graph"))
    run = json.loads(rt.handle_begin_run({"objective": "prefs"}))
    content = "I prefer TypeScript for frontend."
    art = rt.store().upsert_artifact(
        {
            "artifact_type": "user_note",
            "content": content,
            "content_hash": "h",
            "authority": "user",
            "session_id": "s",
            "turn_id": "t",
            "run_id": run["run_id"],
        }
    )
    quote = "TypeScript"
    start = content.index(quote)
    frag = {
        "summary": "pref",
        "nodes": [
            {
                "temp_id": "p1",
                "node_type": "Preference",
                "label": "Frontend language",
                "summary": "TypeScript",
                "status": "candidate",
                "authority": "user",
                "confidence": 0.95,
                "salience": 0.9,
                "identity_key": "pref.frontend",
                "evidence": [
                    {
                        "artifact_id": art["artifact_id"],
                        "start_char": start,
                        "end_char": start + len(quote),
                        "quote": quote,
                        "relation": "supports",
                        "confidence": 0.95,
                    }
                ],
            }
        ],
        "edges": [],
    }
    sub = json.loads(
        rt.handle_submit_fragment(
            {
                "run_id": run["run_id"],
                "producer_role": "structure",
                "fragment": frag,
            }
        )
    )
    assert sub["success"] is True
    fin = json.loads(
        rt.handle_finalize({"run_id": run["run_id"], "promotion_policy": "strict"})
    )
    assert fin["success"] is True
    node = rt.store().get_node(sub["nodes"][0])
    assert node["status"] == "asserted"


def test_export_path_safety(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rt = SemanticGraphRuntime(config=SemanticGraphConfig(db_subdir="semantic-graph"))
    store = rt.store()
    root = tmp_path / "semantic-graph" / "exports"
    result = export_graph(store, format="json", export_root=root)
    assert Path(result["path"]).exists()
    with pytest.raises(ExportPathError):
        export_graph(
            store,
            format="json",
            export_root=root,
            output_path=str(tmp_path / ".." / "escape.json"),
        )


def test_cli_purge_confirm(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from plugins.semantic_graph.cli import register_cli
    from plugins.semantic_graph.runtime import SemanticGraphRuntime

    rt = SemanticGraphRuntime(config=SemanticGraphConfig(db_subdir="semantic-graph"))

    class Ctx:
        def __init__(self):
            self.setup = None
            self.handler = None

        def register_cli_command(self, **kwargs):
            self.setup = kwargs["setup_fn"]
            self.handler = kwargs["handler_fn"]

    ctx = Ctx()
    register_cli(ctx, rt)
    import argparse

    parser = argparse.ArgumentParser()
    ctx.setup(parser)
    ns = parser.parse_args(["purge", "--before", "2099-01-01", "--confirm", "NOPE"])
    assert ctx.handler(ns) == 2
    ns2 = parser.parse_args(["purge", "--before", "2099-01-01", "--confirm", "PURGE"])
    assert ctx.handler(ns2) == 0
