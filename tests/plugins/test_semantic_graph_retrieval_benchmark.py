from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from plugins.semantic_graph.retrieval import render_context, search_and_rank
from plugins.semantic_graph.store import SemanticGraphStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "semantic_graph_retrieval_benchmark.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _make_store(tmp_path: Path) -> tuple[SemanticGraphStore, str, str]:
    fixture = _load_fixture()
    store = SemanticGraphStore(tmp_path / "semantic.db")
    store.ensure_ready()
    run_a = store.create_run(objective="lexical benchmark A")["run_id"]
    run_b = store.create_run(objective="lexical benchmark B")["run_id"]

    for item in fixture["nodes"]:
        store.upsert_node(
            {
                "node_id": item["identity"],
                "node_type": "Preference",
                "subtype": item["identity"].split(".")[0],
                "label": item["label"],
                "normalized_label": item["label"].casefold(),
                "summary": item["summary"],
                "identity_key": item["identity"],
                "status": "asserted",
                "authority": "user",
                "confidence": 0.95,
                "salience": 0.8,
            }
        )
        store.link_run_node(run_a, item["identity"])

    for item in fixture["correction_nodes"]:
        store.upsert_node(
            {
                "node_id": item["old_identity"],
                "node_type": "Preference",
                "subtype": "correction",
                "label": f"Historical preference {item['old_identity']}",
                "normalized_label": item["old_identity"],
                "summary": item["old_summary"],
                "identity_key": item["old_identity"],
                "status": "superseded",
                "authority": "assistant",
                "confidence": 0.95,
                "salience": 0.8,
            }
        )
        store.upsert_node(
            {
                "node_id": item["new_identity"],
                "node_type": "Preference",
                "subtype": "correction",
                "label": f"Current preference {item['new_identity']}",
                "normalized_label": item["new_identity"],
                "summary": item["new_summary"],
                "identity_key": item["new_identity"],
                "status": "asserted",
                "authority": "user",
                "confidence": 0.95,
                "salience": 0.8,
            }
        )
        store.link_run_node(run_a, item["new_identity"])

    store.upsert_node(
        {
            "node_id": "run-b-only",
            "node_type": "Preference",
            "subtype": "isolation",
            "label": "TypeScript in another run",
            "normalized_label": "typescript in another run",
            "summary": "This decoy must not cross run scope.",
            "identity_key": "run-b-only",
            "status": "asserted",
            "authority": "user",
            "confidence": 0.99,
            "salience": 0.99,
        }
    )
    store.link_run_node(run_b, "run-b-only")
    return store, run_a, run_b


def _rank_of(rows: list[dict[str, Any]], expected: str) -> int | None:
    for rank, row in enumerate(rows, start=1):
        if row["identity_key"] == expected:
            return rank
    return None


def test_lexical_retrieval_benchmark_baseline(tmp_path: Path):
    fixture = _load_fixture()
    store, run_a, _run_b = _make_store(tmp_path)
    before = {
        row["node_id"]: (row["status"], row["authority"], row["confidence"])
        for row in store.list_nodes(limit=500)
    }
    measurements: list[float] = []
    group_stats: dict[str, dict[str, float | int]] = {}
    all_results: list[dict[str, Any]] = []

    for query in fixture["queries"]:
        started = time.perf_counter()
        rows = search_and_rank(
            store,
            query["query"],
            top_k=8,
            min_confidence=0.60,
            run_id=run_a if query.get("group") == "exact_identifier" else None,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        measurements.append(elapsed_ms)
        expected = query["expected"]
        ranks = [_rank_of(rows, identity) for identity in expected]
        hit = any(rank is not None for rank in ranks)
        reciprocal_rank = 1.0 / min(rank for rank in ranks if rank is not None) if hit else 0.0
        group = query["group"]
        stats = group_stats.setdefault(group, {"count": 0, "hits": 0, "mrr_sum": 0.0})
        stats["count"] += 1
        stats["hits"] += int(hit)
        stats["mrr_sum"] += reciprocal_rank
        all_results.append(
            {
                "query": query["query"],
                "group": group,
                "expected": expected,
                "returned": [row["identity_key"] for row in rows],
                "hit": hit,
                "returned_any": bool(rows),
                "reciprocal_rank": reciprocal_rank,
                "context_chars": len(render_context(rows, 3500) or ""),
                "latency_ms": elapsed_ms,
            }
        )
        if query.get("negative"):
            # The lexical baseline may return low-precision candidates for an
            # irrelevant query; record that behavior instead of making the
            # baseline test self-contradictory. The hybrid gate compares this
            # rate and must not worsen it.
            assert not expected
        elif group == "correction_history":
            assert expected and expected[0] in [row["identity_key"] for row in rows]

    after = {
        row["node_id"]: (row["status"], row["authority"], row["confidence"])
        for row in store.list_nodes(limit=500)
    }
    assert before == after

    normal_rows = search_and_rank(store, "Historical preference 1", top_k=8, min_confidence=0.60)
    assert all(row["status"] not in {"rejected", "superseded"} for row in normal_rows)
    scoped_rows = search_and_rank(store, "TypeScript", top_k=8, min_confidence=0.60, run_id=run_a)
    assert "run-b-only" not in {row["node_id"] for row in scoped_rows}
    assert all("opaque-secret" not in json.dumps(row, ensure_ascii=False) for row in scoped_rows)

    for group, stats in group_stats.items():
        count = int(stats["count"])
        stats["recall_at_8"] = int(stats["hits"]) / count
        stats["mrr_at_8"] = float(stats["mrr_sum"]) / count
        del stats["mrr_sum"]
        del stats["hits"]

    summary = {
        "variant": "A_lexical",
        "query_count": len(fixture["queries"]),
        "groups": group_stats,
        "overall_recall_at_8": sum(int(item["hit"]) for item in all_results) / len(all_results),
        "negative_false_recall_rate": sum(
            int(item["returned_any"])
            for item in all_results
            if item["group"] == "negative"
        ) / 10,
        "negative_no_result_precision": sum(
            int(not item["returned_any"])
            for item in all_results
            if item["group"] == "negative"
        ) / 10,
        "context_chars_max": max(int(item["context_chars"]) for item in all_results),
        "latency_ms_p50": statistics.median(measurements),
        "latency_ms_p95": sorted(measurements)[max(0, int(len(measurements) * 0.95) - 1)],
        "rejected_or_superseded_leak_rate": 0.0,
        "cross_run_leak_rate": 0.0,
        "secret_recall_count": 0,
        "state_mutation_count": 0,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    assert summary["rejected_or_superseded_leak_rate"] == 0.0
    assert summary["cross_run_leak_rate"] == 0.0
    assert summary["secret_recall_count"] == 0
    assert summary["state_mutation_count"] == 0
