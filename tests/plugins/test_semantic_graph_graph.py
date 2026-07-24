"""Sanitize + graph validation/merge tests."""

from __future__ import annotations

from pathlib import Path

from plugins.semantic_graph import graph
from plugins.semantic_graph.sanitize import normalize_key, normalize_text, sanitize_text
from plugins.semantic_graph.store import SemanticGraphStore


def test_nfkc_and_redaction():
    assert normalize_text("  ＡＢＣ  ") == "ABC"
    assert normalize_key("Python") == "python"
    cleaned = sanitize_text(
        "api_key=sk-abcdefghijklmnopqrstuvwxyz012345 "
        "Authorization: Bearer tokensecretvalue123456 "
        "user@example.com "
        r"C:\Users\alice\project "
        "10.0.0.5 "
        "\u200bhidden",
        max_chars=500,
    )
    assert "sk-" not in cleaned.text
    assert "Bearer [REDACTED]" in cleaned.text or "[REDACTED]" in cleaned.text
    assert "[EMAIL_REDACTED]" in cleaned.text
    assert "~" in cleaned.text
    assert "[PRIVATE_IP_REDACTED]" in cleaned.text
    assert "\u200b" not in cleaned.text
    assert cleaned.redaction_count >= 1


def test_truncation():
    cleaned = sanitize_text("x" * 100, max_chars=20)
    assert cleaned.truncated is True
    assert len(cleaned.text) <= 20


def test_evidence_and_self_loop(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SemanticGraphStore(tmp_path / "sg.db")
    store.ensure_ready()
    content = "I prefer TypeScript for frontend."
    art = store.upsert_artifact(
        {
            "artifact_type": "user_note",
            "content": content,
            "content_hash": "h",
            "authority": "user",
            "session_id": "s",
            "turn_id": "1",
        }
    )
    quote = "TypeScript"
    start = content.index(quote)
    end = start + len(quote)
    good = {
        "summary": "prefs",
        "nodes": [
            {
                "temp_id": "n1",
                "node_type": "Preference",
                "label": "Frontend language",
                "summary": "TypeScript preferred",
                "status": "asserted",
                "authority": "user",
                "confidence": 0.9,
                "salience": 0.8,
                "identity_key": "pref.frontend.lang",
                "evidence": [
                    {
                        "artifact_id": art["artifact_id"],
                        "start_char": start,
                        "end_char": end,
                        "quote": quote,
                        "relation": "supports",
                        "confidence": 0.9,
                    }
                ],
            }
        ],
        "edges": [],
    }
    ok = graph.validate_fragment(good, store)
    assert ok["valid"] is True

    bad_offset = {
        "summary": "x",
        "nodes": [
            {
                "temp_id": "n1",
                "node_type": "Claim",
                "label": "x",
                "summary": "x",
                "status": "candidate",
                "authority": "assistant",
                "confidence": 0.5,
                "salience": 0.5,
                "evidence": [
                    {
                        "artifact_id": art["artifact_id"],
                        "start_char": 0,
                        "end_char": 3,
                        "quote": "NOPE",
                        "relation": "supports",
                        "confidence": 0.5,
                    }
                ],
            }
        ],
        "edges": [],
    }
    bad = graph.validate_fragment(bad_offset, store)
    assert bad["valid"] is False

    loop = {
        "summary": "x",
        "nodes": [
            {
                "temp_id": "n1",
                "node_type": "Claim",
                "label": "x",
                "summary": "x",
                "status": "candidate",
                "authority": "assistant",
                "confidence": 0.5,
                "salience": 0.5,
                "evidence": [],
            }
        ],
        "edges": [
            {
                "source_temp_id": "n1",
                "target_temp_id": "n1",
                "edge_type": "relates_to",
                "strength": "weak",
                "confidence": 0.5,
                "status": "candidate",
                "rationale": "",
                "evidence": [],
            }
        ],
    }
    loop_v = graph.validate_fragment(loop, store)
    assert loop_v["valid"] is False
    assert any("self-loop" in e for e in loop_v["errors"])


def test_apply_duplicate_and_contradiction(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = SemanticGraphStore(tmp_path / "sg.db")
    store.ensure_ready()
    run = store.create_run(objective="t")
    content = "A supports B. Also contradicts."
    art = store.upsert_artifact(
        {
            "artifact_type": "document",
            "content": content,
            "content_hash": "h2",
            "authority": "external",
            "session_id": "s",
            "turn_id": "2",
        }
    )
    frag = {
        "summary": "claims",
        "nodes": [
            {
                "temp_id": "a",
                "node_type": "Claim",
                "label": "Claim A",
                "summary": "A",
                "status": "candidate",
                "authority": "assistant",
                "confidence": 0.6,
                "salience": 0.5,
                "identity_key": "claim.a",
                "evidence": [],
            },
            {
                "temp_id": "b",
                "node_type": "Claim",
                "label": "Claim B",
                "summary": "B",
                "status": "candidate",
                "authority": "assistant",
                "confidence": 0.6,
                "salience": 0.5,
                "identity_key": "claim.b",
                "evidence": [],
            },
        ],
        "edges": [
            {
                "source_temp_id": "a",
                "target_temp_id": "b",
                "edge_type": "contradicts",
                "strength": "medium",
                "confidence": 0.7,
                "status": "candidate",
                "rationale": "explicit contradiction in source",
                "evidence": [],
            }
        ],
    }
    r1 = graph.apply_fragment_to_store(
        store, run["run_id"], frag, producer_role="structure"
    )
    assert r1["success"] is True
    r2 = graph.apply_fragment_to_store(
        store, run["run_id"], frag, producer_role="structure"
    )
    assert r2["duplicate"] is True
    edges = store.list_edges(include_rejected=True)
    assert any(e["edge_type"] == "contradicts" for e in edges)
    # Both nodes retained
    assert store.get_node(r1["nodes"][0]) is not None
    assert store.get_node(r1["nodes"][1]) is not None


def test_stable_edge_id():
    a = graph.make_edge_id("n1", "n2", "supports", "")
    b = graph.make_edge_id("n1", "n2", "supports", "")
    assert a == b
    assert a.startswith("edge_")
